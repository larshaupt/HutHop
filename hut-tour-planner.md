# Hut Tour Planner: Project Brief

A private web app to browse and filter Swiss mountain huts (SAC plus selected non-SAC huts) and to **suggest multi-day summer hut tours** from user constraints, including availability checks and price estimates.

> Status: Phases 0–3 done; Phase 4 availability done, prices postponed. Items marked **(verify)** are assumptions or partial observations and must be checked before relying on them.

---

## 1. Requirements

### Scope

- **Private project** (single user / friends). Not public, not commercial. See the legal notes in §6.
- **Region:** Switzerland. SAC huts first, plus selected non-SAC huts added manually.
- **Activity:** summer hiking only, on the official signposted trail network. Difficulty uses the official trail categories (yellow / red-white / blue-white), with a user-selectable maximum.
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
  - max trail category (yellow / red-white / blue-white)
  - start/end region or specific public transport stops, including cable car and lift stations; loop vs. point-to-point
  - catering preference (self-catering / half board / don't care)
  - number of people and member status (for price and availability)
- Output: ranked list of tour suggestions (stop → hut → … → hut → stop), each with:
  - per-day time, distance, ascent/descent and max trail category (plus the SAC grade and SAC time where a matching SAC route exists)
  - route on the map
  - price estimate
  - availability status per night

**F3: Availability**
- Free beds and daily status per hut and night from hut-reservation.org. One request per hut returns the whole season, cached for a few hours.
- Used as a **filter during tour search** (full or closed huts are dropped), not only as a final check.
- Distinguish full, closed, not yet released (0 of 0 beds) and unknown (hut not on hut-reservation.org).

**F4: Price estimate**
- Computed from stored price components and user inputs: members/non-members, half board vs. self-catering vs. overnight only, number of people, nights, local tax.

**F5: Data maintenance** (done: `refresh` command, "Edit hut data", "+ Add hut")
- Periodic sync of SAC hut data.
- Manual entry and correction of prices, missing fields and non-SAC huts. Manual values must survive re-syncs.
- Review tool for computed route edges, with the ability to exclude bad ones.

**F6 (later)**
- GPX export (for the swisstopo app), saved tours, offline/PWA.

### Non-functional requirements

- Right-sized: SQLite, a single backend service, minimal frontend. No framework or microservices unless needed.
- Be a polite API consumer: low request rates, caching, an identifiable User-Agent, no bulk hammering.
- Track the provenance of every data value (`source`, `updated_at`).
- Runs locally with `uv run`. Docker Compose only once it is deployed to a home server.

---

## 2. System design

```
                ┌──────────────────────────── Web frontend ───────────────────────────┐
                │ MapLibre + swisstopo tiles · filters · tour form · results          │
                └──────────────────────────────────┬──────────────────────────────────┘
                                                   │ JSON/HTTP
                ┌──────────────────────────────────▼──────────────────────────────────┐
                │ Backend (Python, FastAPI)                                           │
                │  • /huts (filtering)  • /tours/suggest  • /availability  • /admin   │
                │  • tour search over the place graph  • price estimator              │
                └───────────────┬─────────────────────────────────────┬───────────────┘
                                │                                     │ one request per hut
                 ┌──────────────▼──────────────┐          ┌───────────▼──────────────┐
                 │ SQLite DB                   │          │ hut-reservation.org      │
                 │ huts, stops, place graph    │          │ (availability, cached    │
                 │ (edges + route lines),      │          │  for a few hours)        │
                 │ prices, availability cache  │          └──────────────────────────┘
                 └──────────────▲──────────────┘
                                │ offline batch jobs (Python CLI)
        ┌───────────────────────┴────────────────────────────────────────┐
        │  • sync_sac: SAC API → place/hut/hut_month                     │
        │  • load_stops: official stop list (incl. cable cars) → stop    │
        │  • build_edges: swissTLM3D trail graph (in memory only) →      │
        │    searches outward from every hut → edge (+ route lines)      │
        └────────────────────────────────────────────────────────────────┘
```

### Data strategy

| Data | Change rate | Approach |
|---|---|---|
| Hut base data (location, beds, opening, catering, services) | rare | Sync job from the SAC API (e.g. weekly) into the DB |
| Prices, missing details, non-SAC huts | rare, no API | Manual entry / scraping into the DB |
| Bed availability | constant | One request per hut returns the whole season. Cached for a few hours and used as a search filter |
| Hut↔hut and stop↔hut connections | almost never | Precomputed offline on the swissTLM3D trail graph (rebuilt yearly with each new edition) |

**Key principles**
- Keep **source data separate from manual edits** (`hut_override`). The app reads "override if present, else source" (view `hut_effective`).
- Store the **raw API JSON** so fields can be re-parsed without re-fetching.
- Store prices as **components**, not totals. Compute estimates per request.
- Tour suggestions and price estimates are **computed, not stored** (until saved tours exist).
- **Offline build and app are separate.** The full trail graph (~389k nodes) only exists inside `build_edges`. The app only reads the small place graph (huts + stops) and simplified route lines from SQLite.
- Store **LV95 coordinates** next to WGS84. All Swiss sources use LV95, and the map uses WGS84.

### Routing approach

Decided in Phase 0 (see `spikes/routing_findings.md`): own routing on the official swissTLM3D hiking network.

1. **Trail graph:** build a directed graph from the swissTLM3D hiking lines (3D) inside the build job. Snap every hut and stop to its nearest trail node and store the snap distance. Snaps over ~200 m are flagged for review; huts over 500 m away (glacier/high-alpine huts, huts abroad) and stops over 300 m away get no edges.
2. **Search outward from each hut**, not pair by pair: from every hut, run Dijkstra by hiking time up to a cutoff (e.g. 10 h), forward (hut → X) and on the reversed graph (X → hut). Every hut and stop reached becomes a directed edge. This covers hut→hut, stop→hut and hut→stop without a straight-line radius and without searches from the many stops.
3. **Hiking time** with the Swiss hiking-time formula (Schweizer Wanderwege, "Wanderzeitberechnung" 2020.2): minutes per km as a polynomial of slope, applied per ~20 m piece using the swissTLM3D elevations.
4. **Difficulty = trail category:** 1 = yellow (≈ T1), 2 = red-white (≈ T2–T3, sometimes T4), 3 = blue-white (≈ T4–T6). The searches run once per category limit (yellow only, up to red-white, up to blue-white), so each edge is the fastest path *within* that limit. An edge is only stored for a higher limit if it differs from the easier one.
5. Store **directed** edges (A→B ≠ B→A) with a simplified route line (e.g. 10 m tolerance, encoded polyline).
6. **SAC routes are shown, not used as overrides (for now):** where an SAC route matches an edge, show its time and grade next to the computed values. Computed times already match SAC times (median ratio 0.99), so automatic overrides are deferred.
7. `excluded` flag to veto bad edges (closed paths, glacier crossings, etc.).

### Tour search

- Graph: nodes = `place` (huts and stops), edges = non-excluded edges for the user's max trail category.
- **Before the search:** find the candidate huts in the area, fetch their season availability (cached), and drop huts that are full or closed on every relevant night.
- **Hut usable on a date:** prefer hut-reservation.org's daily status (SERVICED = catered, UNSERVICED = self-service only, CLOSED) and free beds ≥ group size. Otherwise fall back to the SAC monthly codes (`catering`, `self_service`). `1` (partly) means "check dates", and huts with no data are "unknown": allowed but ranked lower and flagged.
- Bounded DFS / beam search: start stop → N huts (one per night) → end stop. Each day's time must fall within [min, max] hours, and the catering preference must match on that date.
- Ranking (`app/planner/ranking.py`): each tour gets sub-scores 0–1 (day length fit, confirmed free beds, opening-data certainty, easy trails, hut altitude, short approach). "Best" is their weighted average with **fixed weights from `huthop.toml`** (not changeable in the UI). Other sorts: day length, shortest, least ascent, highest huts, most free beds. The beam search still generates candidates by day-length fit, then the complete tours are re-ranked and diversified.

### Data model (SQLite)

```sql
-- Places: everything a tour can start, end or stay at
CREATE TABLE place (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('hut','stop')),
  name        TEXT NOT NULL,
  e_lv95      REAL NOT NULL,          -- Swiss LV95 (EPSG:2056), as in all sources
  n_lv95      REAL NOT NULL,
  lat         REAL NOT NULL,          -- WGS84, for the map
  lon         REAL NOT NULL,
  altitude_m  INTEGER,
  snap_m      REAL                    -- distance to the trail network (build_edges QA)
);

CREATE TABLE hut (
  place_id     INTEGER PRIMARY KEY REFERENCES place(id),
  sac_id       INTEGER UNIQUE,         -- NULL for non-SAC huts
  hrs_id       INTEGER,                -- hut-reservation.org id
  operator     TEXT,                   -- 'sac', 'private', ...
  owner        TEXT,
  beds         INTEGER,
  shelter_beds INTEGER,                -- winter room / emergency shelter places
  website      TEXT,
  description  TEXT,
  services     TEXT,                   -- JSON
  photos       TEXT,                   -- JSON: caption, copyright, thumbnail URLs
  routes       TEXT,                   -- JSON: free SAC access routes only
  source       TEXT NOT NULL,          -- 'sac_api' | 'manual' | 'scraped'
  raw          TEXT,                   -- original API JSON
  synced_at    TEXT
);

CREATE TABLE hut_month (
  hut_id    INTEGER REFERENCES hut(place_id),
  month     INTEGER CHECK (month BETWEEN 1 AND 12),
  self_service INTEGER,                -- SAC `opening`: winter room / self-service open, 0 yes / 1 partly / 2 no
  catering     INTEGER,                -- SAC `catering`: catered, 0 yes / 1 partly / 2 no
                                       -- all months 2/2 = no data (unknown), not "closed"
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
  uic       INTEGER UNIQUE,            -- official stop number (DiDok/UIC)
  mode      TEXT                       -- comma list: bus, train, tram, metro, rack_railway, funicular,
                                       -- cableway, chairlift, boat, elevator
);

CREATE TABLE edge (
  id             INTEGER PRIMARY KEY,
  from_place     INTEGER NOT NULL REFERENCES place(id),
  to_place       INTEGER NOT NULL REFERENCES place(id),
  distance_m     INTEGER,
  ascent_m       INTEGER,
  descent_m      INTEGER,
  time_min       INTEGER,              -- hiking time in this direction (Swiss formula)
  max_category   INTEGER NOT NULL,     -- 1 yellow, 2 red-white, 3 blue-white (hardest along path)
  category_limit INTEGER NOT NULL,     -- search limit this edge was found with (1..3)
  source         TEXT NOT NULL,        -- 'tlm_routed' | 'manual'
  sac_route_id   INTEGER,              -- matching SAC route, shown for reference
  sac_time_min   INTEGER,
  sac_grade      TEXT,                 -- e.g. 'T3+'
  geometry       TEXT,                 -- simplified encoded polyline
  excluded       INTEGER DEFAULT 0,
  note           TEXT,
  computed_at    TEXT,
  UNIQUE (from_place, to_place, category_limit, source)
);
CREATE INDEX edge_from ON edge(from_place) WHERE excluded = 0;

CREATE TABLE availability (
  hut_id      INTEGER REFERENCES hut(place_id),
  date          TEXT,
  hut_status    TEXT,                  -- 'SERVICED' | 'UNSERVICED' | 'CLOSED'
  free_beds     INTEGER,
  total_places  INTEGER,               -- 0 with free_beds 0 = not released yet, not "full"
  fetched_at    TEXT,
  PRIMARY KEY (hut_id, date)
);
```

Planned views:
- `hut_effective`: source data merged with overrides.
- `edge_best`: one edge per (from, to, category limit); `manual` wins over `tlm_routed`.

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

- Base maps: vector style `https://vectortiles.geo.admin.ch/styles/ch.swisstopo.lightbasemap.vt/style.json`, raster WMTS `ch.swisstopo.pixelkarte-farbe` (3857), trail overlay WMTS `ch.swisstopo.swisstlm3d-wanderwege`.
- Terms ([FSDI general terms](https://www.geo.admin.ch/en/general-terms-of-use-fsdi)): free, no registration, fair use (a web app with up to ~20,000 users/day counts as fair use). Credit "© swisstopo".
- swissTLM3D: official hiking trail network with categories (yellow / white-red-white / white-blue-white).
- swissALTI3D: elevation model.
- geo.admin REST API, e.g. winter accommodations layer `ch.swisstopo.unterkuenfte-winter` (made with the SAC, licence "open use, must provide the source"). Useful as an open hut-location source.
- The swisstopo app has no public routing API. Use it for manual cross-checking and as a GPX target.

### 3.5 Public transport stops

- Official stop list: GeoAdmin STAC collection `ch.bav.haltestellen-oev` (LV95 CSV export, `PointExploitation.csv`). See via-helvetica `docs/PUBLIC_TRANSPORT_DATA_PIPELINE.md`.
- Used as tour start/end nodes. Includes cable car, gondola and lift stations, which are key for hut tours (start or end at a top station). GeoPackage layer `Betriebspunkt`: `Nummer` (stop number), `Betriebspunkttyp_Bezeichnung` (keep `Haltestelle*`), `Verkehrsmittel_Code` (A bus, B train, C tram, D metro, E rack railway, F funicular, G cable car, H chairlift, I boat, J elevator).

### 3.6 Coordinate conversion

- SAC, swissTLM3D and the stop list all use LV95 (EPSG:2056). Store LV95 and convert to WGS84 (EPSG:4326) for the map with `pyproj`.

---

## 4. Development plan

### Phase 0: Spikes (1–2 evenings)

Answer the open questions before building:
- [x] SAC API: pagination parameter, detail endpoint, meaning of the 0/1/2 codes. → `spikes/sac_findings.md`
- [x] hut-reservation.org: availability endpoint, auth, response format, prices. → `spikes/hut_reservation_findings.md`
- [x] Routing: GraphHopper/OSM vs. swissTLM3D on 17 hut pairs, compared with SAC times. **swissTLM3D chosen.** → `spikes/routing_findings.md`

### Phase 1: Data + map (first usable version)

- [x] Project skeleton: FastAPI, SQLite, run with `uv` (no Docker yet).
- [x] `sync_sac` CLI: SAC API → `place`, `hut`, `hut_month` (+ LV95→WGS84, raw JSON).
- [x] `/api/huts` endpoint with filters, `/api/huts/{id}` detail.
- [x] Frontend: MapLibre map, swisstopo tiles (light vector / topo raster / trail overlay), hut layer coloured by month status, filter panel, list, detail view, mobile bottom sheet, state in the URL hash.

### Phase 2: Route graph

- [x] `load_stops`: official stop list (incl. cable cars and lifts) into `place`/`stop`. 27,325 passenger stops.
- [x] `build_edges`: swissTLM3D trail graph → snap huts and stops → outward searches from every hut (forward + reverse, per category limit, SciPy Dijkstra) → `edge` with route lines. ~31,000 edges in ~2 min; the 40 fastest stops per hut and direction are kept (an earlier rule that dropped stops behind other stops removed village stations such as Grindelwald).
- [x] Show matching SAC route time and grade next to computed edges (name matching as in the spike, 37 matches).
- [x] Edge review in the frontend: connections of the selected hut on the map and in the detail view (per trail limit), toggle `excluded`; snap warnings for huts > 200 m from a trail.
- Huts more than 500 m from a signposted trail (54, mostly glacier/high-alpine huts and huts abroad) get no computed edges.

### Phase 3: Tour suggestions

- [x] Tour search (beam search, width 3000) with date-aware catering/self-service checks (SAC monthly codes). All edge variants up to the trail limit are alternatives, so a harder limit only adds options; a small per-day penalty prefers easier trails between equal tours.
- [x] `POST /api/tours/suggest` and `GET /api/stops` (start picker). Inputs: date (not in the past), nights, hours per day, hardest trail, catering, map area, start near a stop (1.5 km), back to start (3 km), include unknown huts.
- [x] "Plan a tour" tab: form, result cards with day bars, tour detail timeline, route on the map with numbered huts; state in the URL.

### Phase 4: Prices + availability

- [x] Availability: season calendar per hut from hut-reservation.org, **cached 10 minutes** (availability changes quickly). Tour search runs in up to 3 rounds: search, fetch the calendars of huts in the best 25 candidates (max 40 fetches, 2 in parallel), search again. Full or closed nights drop out; free-bed nights use the daily status (SERVICED = catered, UNSERVICED = self-service); not-yet-released (0 of 0) and huts without online booking fall back to SAC monthly codes and rank slightly lower. Typical search: 3–6 s with fresh fetches.
- [x] Planner: number of people, "check free beds" option, card badge (beds free / N nights unconfirmed), free beds + "Book" link per night.
- [x] A CLOSED 0-of-0 night before the hut's released season counts as closed (e.g. Chamanna Cluozza before 3 June), not "not bookable yet". Only nights with nothing released later stay "not bookable yet".
- [ ] Price entry (start with YAML/CSV import, then a simple admin form). **Postponed.**
- [ ] Price estimator from user inputs. **Postponed.**

### Later

- ~~GPX export~~ done: `GET /api/tours/{id}/gpx`, one track per day + waypoints. Tour ids are `date_places_limits` (e.g. `2027-07-17_123-45-67-89_2-2-3`), stable across edge rebuilds, so they also serve sharing, saved tours and the editor.
- Saved tours, non-SAC huts, PWA/offline, better ranking.
- ~~Elevation in GPX~~ done: `edge.elevation` holds the swissTLM3D height of each route point (from `build_edges`, or `app.ingest.profiles` for older edges); used for `<ele>` in GPX and a height profile per day in the tour view.
- ~~Free-beds calendar~~ done: `GET /api/huts/{id}/availability?people=N`, a month calendar in the hut details (same 10-minute cache as tour checks).
- **Export to Komoot and the swisstopo app.** Neither seems to have a public import API (Komoot's API is for partners only; no documented import link for the swisstopo app; check both before building), so both go through GPX files. Komoot: one GPX per day (it imports one tour per file), then its upload page (komoot.com/upload). swisstopo app: on a phone, "share" the GPX to the app, which imports it as a track. map.geo.admin.ch can show a GPX by URL only if the file is publicly reachable, which HutHop isn't (tailnet only). Needs elevation in the GPX, so both apps show correct ascent.
- **Custom sorting and a "best" score** for tour suggestions (user-defined quality metric).
- **Tour editor:** build a tour by hand or edit a suggestion (swap a hut, add/remove a night, change start/end). Each day is a stored edge, so editing = choosing another edge from the hut's connections; recheck opening and free beds after each change.
- SAC times as automatic overrides (per-grade calibration for T4+).
- **Public transport within a tour:** allow a day to include a bus/train/cable car ride, e.g. walk down from a hut, take the bus through the valley, walk up to the next hut. Approach: combine a hut→stop edge, a timetable ride (or a simple stop→stop transfer table from the official timetable data), and a stop→hut edge into one day, counting only the walking time against the daily limit. Needs timetable data (e.g. opentransportdata.swiss GTFS) and a cap on rides per day.
- Warnings for official trail closures and military shooting zones (geo.admin layers, see via-helvetica).
- Docker Compose for a home server.

---

## 5. Proposed stack and repo layout

- **Backend:** Python 3.12, FastAPI, `httpx`, `pyproj`, SQLite (via `sqlite3` or SQLModel)
- **Routing:** own graph on swissTLM3D (pyogrio, shapely, networkx)
- **Frontend:** plain HTML/JS + MapLibre GL (no framework initially)
- **Ops:** `uv run` locally. Docker Compose later, for a home server.

```
hut-planner/
├── app/
│   ├── api/            # FastAPI routes
│   ├── db/             # schema.sql, migrations, queries
│   ├── ingest/         # sync_sac.py, load_stops.py, build_edges.py (offline jobs)
│   ├── planner/        # tour search, ranking, hiking time, price estimator
│   └── clients/        # sac.py, hut_reservation.py, geoadmin.py
├── web/                # static frontend
├── data/               # sqlite db, swissTLM3D, manual price files (gitignored as needed)
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
- Computed times are less reliable for T4+ (official times there are hand-adjusted). Show the SAC time next to the computed one where available.
- Huts far from the signposted network (long snaps) can get wrong or missing edges. Review snaps over ~200 m.
- The monthly SAC codes are coarse. Prefer hut-reservation.org's daily status where it exists.
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
