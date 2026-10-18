# Hut Tour Planner: Project Brief

A private web app to browse and filter Swiss mountain huts (SAC plus selected non-SAC huts) and to **suggest multi-day summer hut tours** from user constraints, including availability checks and price estimates.

> Status: Phase 0 (spikes) done, Phase 1 next. Items marked **(verify)** are assumptions or partial observations and must be checked before relying on them.

---

## 1. Requirements

### Scope

- **Private project** (single user / friends). Not public, not commercial. See the legal notes in §6.
- **Region:** Switzerland. SAC huts first, plus selected non-SAC huts added manually.
- **Activity:** summer hiking only, on marked trails (T1–T6 scale, user-selectable maximum).
- **Platform:** web app (desktop and mobile browser).

### Functional requirements

**F1: Hut browser**
- Map of all huts on swisstopo base maps.
- Filters:
  - open in a given month or date range
  - self-catering possible
  - catered / half board available
  - number of beds
  - altitude
  - price range
  - SAC vs. non-SAC
- Hut detail view: description, opening/catering per month, services, links, access routes.

**F2: Tour suggestions** (core feature)
- Inputs:
  - start date, number of days / nights
  - max hiking hours per day (and optionally min hours)
  - max difficulty (T1–T6)
  - start/end region or specific public transport stops; loop vs. point-to-point
  - catering preference (self-catering / half board / don't care)
  - number of people and member status (for price and availability)
- Output: ranked list of tour suggestions (stop → hut → … → hut → stop), each with:
  - per-day time, distance, ascent/descent and max difficulty
  - route on the map
  - price estimate
  - availability status per night

**F3: Availability check**
- Live check of free beds per hut and night for suggested tours (where the hut uses hut-reservation.org).
- Clearly flag huts with unknown availability.

**F4: Price estimate**
- Computed from stored price components and user inputs: members/non-members, half board vs. self-catering vs. overnight only, number of people, nights, local tax.

**F5: Data maintenance**
- Periodic sync of SAC hut data.
- Manual entry and correction of prices, missing fields and non-SAC huts. Manual values must survive re-syncs.
- Review tool for computed route edges, with the ability to exclude bad ones.

**F6 (later)**
- GPX export (for the swisstopo app), saved tours, offline/PWA.

### Non-functional requirements

- Right-sized: SQLite, a single backend service, minimal frontend. No framework or microservices unless needed.
- Be a polite API consumer: low request rates, caching, an identifiable User-Agent, no bulk hammering.
- Track the provenance of every data value (`source`, `updated_at`).
- Runs locally or on a home server via Docker Compose.

---

## 2. System design

```
                ┌──────────────────────────── Web frontend ───────────────────────────┐
                │ MapLibre/Leaflet + swisstopo tiles · filters · tour form · results  │
                └──────────────────────────────────┬──────────────────────────────────┘
                                                   │ JSON/HTTP
                ┌──────────────────────────────────▼──────────────────────────────────┐
                │ Backend (Python, FastAPI)                                           │
                │  • /huts (filtering)  • /tours/suggest  • /availability  • /admin   │
                │  • tour search over route graph  • price estimator                  │
                └───────┬──────────────────────┬───────────────────────┬──────────────┘
                        │                      │                       │ live, cached
                 ┌──────▼──────┐        ┌──────▼───────┐       ┌───────▼──────────────┐
                 │ SQLite DB   │        │ Trail graph  │       │ hut-reservation.org  │
                 │ huts, edges │        │ (swissTLM3D  │       │ (availability)       │
                 │ prices, ... │        │  hiking net) │       └──────────────────────┘
                 └──────▲──────┘        └──────▲───────┘
                        │ batch jobs           │
        ┌───────────────┴───────────────────────┴────────────────────────┐
        │ Ingestion / batch jobs (Python CLI)                            │
        │  • sync_sac: SAC API → place/hut/hut_month                     │
        │  • load_stops: public transport stops → place/stop             │
        │  • build_edges: swissTLM3D routes for nearby pairs → edge      │
        │  • match_sac_routes: prefer SAC times/difficulty where present │
        └────────────────────────────────────────────────────────────────┘
```

### Data strategy

| Data | Change rate | Approach |
|---|---|---|
| Hut base data (location, beds, opening, catering, services) | rare | Sync job from the SAC API (e.g. weekly) into the DB |
| Prices, missing details, non-SAC huts | rare, no API | Manual entry / scraping into the DB |
| Bed availability | constant | Live query at planning time, short cache (minutes) |
| Hut↔hut and stop↔hut connections | almost never | Precomputed with a routing engine and stored |

**Key principles**
- Keep **source data separate from manual edits** (`hut_override`). The app reads "override if present, else source" (view `hut_effective`).
- Store the **raw API JSON** so fields can be re-parsed without re-fetching.
- Store prices as **components**, not totals. Compute estimates per request.
- Tour suggestions and price estimates are **computed, not stored** (until saved tours exist).

### Routing approach

1. Candidate pairs: hut↔hut within about 25 km straight-line distance, and stop↔hut within a radius.
2. Route each pair on the **official swissTLM3D hiking network** (swisstopo, 3D lines, own graph in Python). Difficulty from the trail category: yellow ≈ T1, red-white ≈ T2–T3 (sometimes T4), blue-white ≈ T4–T6. Refine with SAC route grades where available. (Decided in Phase 0 over GraphHopper/OSM, see `spikes/routing_findings.md`.)
3. Hiking time with the **Swiss hiking-time formula** (Schweizer Wanderwege, "Wanderzeitberechnung" 2020.2): minutes per km as a polynomial of slope, applied per ~20 m piece using the swissTLM3D elevations. Calibrate against SAC times.
4. Store **directed** edges (A→B ≠ B→A).
5. Where an SAC route matches, **SAC values override** computed ones. Priority: `sac` > `manual` > `tlm_routed`.
6. `excluded` flag to veto bad edges (closed paths, glacier crossings, etc.).

### Tour search

- Graph: nodes = `place` (huts and stops), edges = non-excluded edges within the user's max difficulty.
- Bounded DFS / beam search: start stop → N huts (one per night) → end stop.
- Each day's time must fall within [min, max] hours, and each hut must be open (and catered/self-catering as required) on that date.
- Ranking (initial): closeness to the target hours per day, total price, no repeated huts, optional "scenic"/altitude bonus.
- Then the availability check on the top-k candidates only (keeps live requests low).

### Data model (SQLite)

```sql
-- Places: everything a tour can start, end or stay at
CREATE TABLE place (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('hut','stop')),
  name        TEXT NOT NULL,
  lat         REAL NOT NULL,          -- WGS84 (converted from LV95)
  lon         REAL NOT NULL,
  altitude_m  INTEGER
);

CREATE TABLE hut (
  place_id     INTEGER PRIMARY KEY REFERENCES place(id),
  sac_id       INTEGER UNIQUE,         -- NULL for non-SAC huts
  hrs_id       INTEGER,                -- hut-reservation.org id
  operator     TEXT,                   -- 'sac', 'private', ...
  beds         INTEGER,
  website      TEXT,
  services     TEXT,                   -- JSON
  source       TEXT NOT NULL,          -- 'sac_api' | 'manual' | 'scraped'
  raw          TEXT,                   -- original API JSON
  synced_at    TEXT
);

CREATE TABLE hut_month (
  hut_id    INTEGER REFERENCES hut(place_id),
  month     INTEGER CHECK (month BETWEEN 1 AND 12),
  open      INTEGER,                   -- SAC code: self-service part open, 0 yes / 1 partly / 2 no
  catering  INTEGER,                   -- SAC code: catered, 0 yes / 1 partly / 2 no
  PRIMARY KEY (hut_id, month)
);

CREATE TABLE hut_price (
  hut_id     INTEGER REFERENCES hut(place_id),
  year       INTEGER NOT NULL,
  item       TEXT NOT NULL CHECK (item IN
               ('overnight','half_board','breakfast','tax','self_catering_fee')),
  member     REAL,                     -- CHF
  non_member REAL,
  youth      REAL,
  source     TEXT NOT NULL,            -- 'manual' | 'scraped' | 'hrs'
  source_url TEXT,
  updated_at TEXT,
  PRIMARY KEY (hut_id, year, item)
);

CREATE TABLE hut_override (
  hut_id     INTEGER REFERENCES hut(place_id),
  field      TEXT NOT NULL,            -- e.g. 'beds', 'catering.7', 'hrs_id'
  value      TEXT NOT NULL,            -- JSON value
  note       TEXT,
  updated_at TEXT,
  PRIMARY KEY (hut_id, field)
);

CREATE TABLE stop (
  place_id  INTEGER PRIMARY KEY REFERENCES place(id),
  sloid     TEXT UNIQUE,               -- official Swiss stop id
  mode      TEXT                       -- 'train' | 'bus' | 'cableway'
);

CREATE TABLE edge (
  id             INTEGER PRIMARY KEY,
  from_place     INTEGER NOT NULL REFERENCES place(id),
  to_place       INTEGER NOT NULL REFERENCES place(id),
  distance_m     INTEGER,
  ascent_m       INTEGER,
  descent_m      INTEGER,
  time_min       INTEGER,              -- hiking time in this direction
  difficulty     INTEGER,              -- 1..6 = T1..T6 (max along path)
  source         TEXT NOT NULL,        -- 'tlm_routed' | 'sac' | 'manual'
  sac_route_id   INTEGER,
  geometry       TEXT,                 -- encoded polyline or GeoJSON
  excluded       INTEGER DEFAULT 0,
  note           TEXT,
  computed_at    TEXT,
  UNIQUE (from_place, to_place, source)
);
CREATE INDEX edge_from ON edge(from_place) WHERE excluded = 0;

CREATE TABLE availability (
  hut_id      INTEGER REFERENCES hut(place_id),
  date        TEXT,
  free_beds   INTEGER,
  fetched_at  TEXT,
  PRIMARY KEY (hut_id, date)
);
```

Planned views:
- `hut_effective`: source data merged with overrides.
- `edge_best`: one edge per (from, to), chosen by source priority.

---

## 3. Known interfaces

### 3.1 SAC route portal API (undocumented, internal)

Observed from the browser on `https://www.sac-cas.ch/en/huts-and-tours/sac-route-portal/?type=hut` (Oct 2026). The site itself is TYPO3. The route portal widget calls a separate JSON API.

**Base:** `https://www.suissealpine.sac-cas.ch/api/1/`

| Endpoint | Purpose |
|---|---|
| `GET poi/search?…&mode=per_discipline&limit=24` | Paged list of POIs (huts). Returns `{results: [...], cursor: "24"}` |
| `GET poi/search_count?…` | `{"count": 384}` (number of huts with the filters below, Oct 2026) |
| `GET poi/search?…&mode=only_ids` | IDs only (likely for map markers) |

Query parameters seen:
```
lang=en&output_lang=en&order_by=-first_time_published&type=hut
&disciplines=mountain_hiking,alpine_tour,climbing,via_ferrata,ski_tour,snowshoe_tour,alpine_climbing
&hut_type=all&mode=per_discipline&limit=24
```

- Pagination: pass the returned value back as `cursor=`. `limit=100` works (4 requests for all huts).
- Auth: none. Cross-origin requests without cookies returned 200.
- Caching: `Cache-Control: max-age=1800`.
- Per-hut detail: `GET sacplus/poi/{id}?output_lang=en` (adds `used_as_destination_in`, `used_as_waypoint_in`, ...). `route/search?id=...` gives route metadata. **No route geometry** without login. Details: `spikes/sac_findings.md`.

**Hut record fields (observed)**
```
id, sac_id, hrs_id, tourism_id, display_name, geographical_name, name_internet,
altitude, geom {type: Point, coordinates: [E, N]}  ← Swiss LV95 / EPSG:2056
sleeps, opening {month_01..month_12: 0|1|2}, catering {month_01..month_12: 0|1|2},
opentext {de,en,fr,it}, services {...}, suitable, addresses, owner, association_id,
emergency_shelter, photos, regions, description, main_lang, type, status, version,
tel, fax, email, url, is_private, routes [...]
+ internal editorial fields (author_id, comments, *_translation_ok, reviewer_ok, ...) → ignore
```

`services` keys seen:
```
cooking_catered, cooking_non_catered, css_rebate, dogs_on_request, drinks,
family_rooms, half_board, internet, not_paid, payment_creditcard, payment_mobile,
plain_meals, separable_group_rooms
```

`routes[]` items (hut access routes, some hut-to-hut):
```json
{"id": 1213, "title": "From the Grimselhospiz", "discipline": "mountain_hiking",
 "main_difficulty": "T5", "normal_route": true, "availability": "free",
 "ascent_altitude": 1000, "ascent_time_min": 420, "ascent_time_max": 480,
 "descent_altitude": 230, "descent_time_min": null, "descent_time_max": null,
 "protection_areas": [{"protection_area_id": 154}], "first_time_published": "..."}
```

Notes:
- **No price fields** in the SAC data.
- `opening`/`catering` codes: **0 = yes (whole month), 1 = partly, 2 = no**. `catering` = staffed/catered (confirmed). `opening` = self-service part (winter room) open (strong hypothesis). All-2 = no data (82 huts).
- `hrs_id` links to hut-reservation.org (0 = none).
- Contact fields (tel, email) are personal data of hut operators. Don't republish.

### 3.2 hut-reservation.org (availability, bookings)

- Used by most SAC huts (and DAV/ÖAV) for reservations.
- From the [domwoe/hut-reservation-mcp](https://github.com/domwoe/hut-reservation-mcp) README:
  - availability lookups are unauthenticated
  - the hut list requires login
  - booking endpoints (`preBook`, `submit`, `myReservations`, …) exist but are undocumented
- Availability: `GET /api/v1/reservation/getHutAvailability?hutId=X&step=WIZARD` (no login, no CSRF) returns the whole booking window per day: `freeBeds`, `totalSleepingPlaces`, `freeBedsPerCategory`, `hutStatus` (SERVICED / UNSERVICED / CLOSED). `0/0` = dates not released yet, not "full".
- Hut info: `GET /api/v1/reservation/hutInfo/{id}` (beds, categories, website). **No prices** in public responses. Details: `spikes/hut_reservation_findings.md`.
- Booking is out of scope. Only read availability.

### 3.3 Routing: swissTLM3D hiking network

- **swissTLM3D Wanderwege** (STAC `ch.swisstopo.swisstlm3d-wanderwege`, GeoPackage, ~200 MB, open data): the official signposted network as 3D lines, with `wanderwege` = Wanderweg / Bergwanderweg / Alpinwanderweg. Builds one connected graph (389k nodes) in about 45 s with pyogrio + shapely + networkx.
- Phase 0 comparison with 17 SAC hut-to-hut routes: Swiss time / SAC time median 0.99, ascent close to SAC. GraphHopper + OSM + SRTM: ascent 2–10× too high, several detours.

### 3.4 swisstopo (open data)

- Base map tiles (WMTS), e.g. `https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg` **(verify layer and terms)**.
- swissTLM3D: official hiking trail network with categories (yellow / white-red-white / white-blue-white).
- swissALTI3D: elevation model.
- geo.admin REST API, e.g. winter accommodations layer `ch.swisstopo.unterkuenfte-winter` (made with the SAC, licence "open use, must provide the source"). Useful as an open hut-location source.
- The swisstopo app has no public routing API. Use it for manual cross-checking and as a GPX target.

### 3.5 Public transport stops

- Official stop list: GeoAdmin STAC collection `ch.bav.haltestellen-oev` (LV95 CSV export, `PointExploitation.csv`). See via-helvetica `docs/PUBLIC_TRANSPORT_DATA_PIPELINE.md`.
- Used as tour start/end nodes.

### 3.6 Coordinate conversion

- SAC uses LV95 (EPSG:2056). Convert to WGS84 (EPSG:4326) with `pyproj`.

---

## 4. Development plan

### Phase 0: Spikes (1–2 evenings)

Answer the open questions before building:
- [x] SAC API: pagination parameter, detail endpoint, meaning of the 0/1/2 codes. → `spikes/sac_findings.md`
- [x] hut-reservation.org: availability endpoint, auth, response format, prices. → `spikes/hut_reservation_findings.md`
- [x] Routing: GraphHopper/OSM vs. swissTLM3D on 17 hut pairs, compared with SAC times. **swissTLM3D chosen.** → `spikes/routing_findings.md`

### Phase 1: Data + map (first usable version)

- [ ] Project skeleton: FastAPI, SQLite, Docker Compose.
- [ ] `sync_sac` CLI: SAC API → `place`, `hut`, `hut_month` (+ LV95→WGS84, raw JSON).
- [ ] `/huts` endpoint with filters.
- [ ] Frontend: MapLibre/Leaflet map, swisstopo tiles, hut markers, filter panel, detail view.

### Phase 2: Route graph

- [ ] `load_stops`: public transport stops into `place`/`stop`.
- [ ] `build_edges`: candidate pairs → swissTLM3D graph → `edge`.
- [ ] `match_sac_routes`: prefer SAC times/difficulty.
- [ ] Edge review in the frontend: show edges on the map, toggle `excluded`.

### Phase 3: Tour suggestions

- [ ] Tour search (bounded DFS / beam search) with date-aware opening/catering checks.
- [ ] `/tours/suggest` endpoint and input form.
- [ ] Results list and route on the map, per-day stats.

### Phase 4: Prices + availability

- [ ] Price entry (start with YAML/CSV import, then a simple admin form).
- [ ] Price estimator from user inputs.
- [ ] Live availability check for top-k suggestions, with caching.

### Later

- GPX export, saved tours, non-SAC huts, PWA/offline, better ranking.

---

## 5. Proposed stack and repo layout

- **Backend:** Python 3.12, FastAPI, `httpx`, `pyproj`, SQLite (via `sqlite3` or SQLModel)
- **Routing:** own graph on swissTLM3D (pyogrio, shapely, networkx)
- **Frontend:** plain HTML/JS + MapLibre GL (no framework initially)
- **Ops:** Docker Compose (app), runs locally or on a home server

```
hut-planner/
├── app/
│   ├── api/            # FastAPI routes
│   ├── db/             # schema.sql, migrations, queries
│   ├── ingest/         # sync_sac.py, load_stops.py, build_edges.py, match_sac_routes.py
│   ├── planner/        # tour search, ranking, price estimator
│   └── clients/        # sac.py, hut_reservation.py, geoadmin.py
├── web/                # static frontend
├── data/               # sqlite db, manual price files (gitignored as needed)
├── docker-compose.yml
└── README.md
```

---

## 6. Constraints, risks and legal notes

**Legal / terms (not legal advice):**
- SAC's [legal information](https://www.sac-cas.ch/en/meta/impressum/legal-information/): site content is copyrighted and "only freely accessible for browsing purposes". Public or commercial use requires SAC's **prior written consent**. Reproduction requires naming sac-cas.ch as the source.
- So: **private use only**, credit SAC as the source, and no redistribution of their data.
- SAC has granted API permission before (e.g. the OSM hut-metadata import, with the condition "moderate usage"). If the project ever goes public, **ask SAC first**.
- Paid route portal content (routes with `availability != "free"`) must not be used.
- hut-reservation.org has its own terms. Read only, low rate, no booking automation.

**Technical risks:**
- Undocumented APIs can change or disappear. Store raw JSON and keep the clients isolated.
- Computed times are less reliable for T4+ (official times there are hand-adjusted). Mark computed edges and prefer SAC data.
- Price data is manual and goes stale. Show `updated_at` and source with every estimate.
- Not all huts use hut-reservation.org. Show availability as "unknown" for those.

**Safety:**
- Suggested tours are planning aids, not guarantees. Show difficulty grades, and remind the user to check conditions, weather and the official sources.

---

## 7. Prior art

| Project | What it does | Relevance |
|---|---|---|
| [domwoe/hut-reservation-mcp](https://github.com/domwoe/hut-reservation-mcp) (MIT) | MCP server: search SAC huts, check availability, prepare bookings via hut-reservation.org | Reference for the hut-reservation.org API |
| [KlaraSaary/find-free-huts](https://github.com/KlaraSaary/find-free-huts) (AGPL) | Finds free DAV/SAC/ÖAV huts for dates, groups nearby huts into multi-day tours (straight-line distance, no UI) | Similar idea, much simpler. Uses session cookies |
| [miloszpp/sac-hut-checker](https://github.com/miloszpp/sac-hut-checker) | Small TypeScript project, no README | Probably an availability checker |
| [OSM SAC hut metadata process](https://wiki.openstreetmap.org/wiki/SAC_hut_metadata_update_process) | OSM import of SAC hut data with SAC permission | Shows the SAC grants API use on request |
| [egofree71/via-helvetica](https://github.com/egofree71/via-helvetica) (MIT) | Browser route planner on swisstopo maps, routing on swissTLM3D, Swiss hiking-time formula, PT stops, trail closures | Reference for swissTLM3D routing, the official time formula and the stop dataset |
| [lailo/bergauf](https://github.com/lailo/bergauf) (MIT + Commons Clause) | MCP server: SwitzerlandMobility routes, elevation, weather (Open-Meteo), PT connections (transport.opendata.ch) | Reference for weather and PT connection APIs |
| SAC app / route portal | Official huts and routes | No custom filters, no automatic tour suggestions |

No existing project combines hut metadata, routing-based tour suggestions, availability and price estimates in one UI.

---

## 8. Open questions

1. ~~Meaning of SAC `opening`/`catering` codes~~ → 0 yes / 1 partly / 2 no (§3.1). Confirm `opening` = winter room with SAC or a few hut websites.
2. ~~SAC API pagination and detail endpoint~~ → `cursor=`, `sacplus/poi/{id}`. No route geometry.
3. ~~hut-reservation.org availability~~ → public `getHutAvailability`, no prices (§3.2).
4. ~~GraphHopper/OSM vs. SAC times~~ → switched to swissTLM3D + Swiss formula (median ratio 0.99). Open: per-grade correction for T4+, which needs more than 17 pairs.
5. Which non-SAC huts to include, and their data source.
6. Tour ranking criteria beyond hours per day and price.
