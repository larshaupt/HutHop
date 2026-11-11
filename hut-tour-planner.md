# Hut Tour Planner: Project Brief

A private web app to browse and filter Swiss mountain huts (SAC plus selected non-SAC huts) and to **suggest multi-day summer hut tours** from user constraints, including availability checks and cost estimates.

> Status (21 Oct 2026): Phases 0–4 done; prices, season dates and contacts read from the hut websites since 21 Oct (§3.7). Deployed on the Oracle VM at huthop.larshauptmann.com behind a Cloudflare Access login since 26 Oct (`docs/deployment.md`). Since Phase 4: GPX export with heights, height profiles, free-beds calendar, manual corrections, "best" ranking with route geometry, About dialog. Items marked **(verify)** are assumptions or partial observations and must be checked before relying on them.

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
  - price range (postponed with prices)
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
  - price estimate (hut costs per person and group)
  - availability status per night

**F3: Availability**
- Free beds and daily status per hut and night from hut-reservation.org. One request per hut returns the whole season, cached for 10 minutes (availability changes quickly).
- Used as a **filter during tour search** (full or closed huts are dropped), not only as a final check.
- Distinguish full, closed, not yet released (0 of 0 beds) and unknown (hut not on hut-reservation.org).

**F4: Price estimate** (done: hut costs from website prices, §3.7)
- Computed from stored price components and user inputs: members/non-members, half board vs. self-catering vs. overnight only, number of people, nights, local tax.

**F5: Data maintenance** (done: `refresh` command, "Edit hut data", "+ Add hut", excluding connections; all behind the admin password)
- Periodic sync of SAC hut data.
- Manual entry and correction of prices, missing fields and non-SAC huts. Manual values must survive re-syncs.
- Review tool for computed route edges, with the ability to exclude bad ones.

**F6 (later)**
- ~~GPX export~~ (done, with heights), ~~saved tours, offline/PWA~~ (done, see §4 Later).

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
                │  • /huts (+ calendar, corrections)  • /tours/suggest, /gpx  • /info │
                │  • tour search over the cached place graph  • admin password        │
                └───────────────┬─────────────────────────────────────┬───────────────┘
                                │                                     │ one request per hut
                 ┌──────────────▼──────────────┐          ┌───────────▼──────────────┐
                 │ SQLite DB                   │          │ hut-reservation.org      │
                 │ huts, stops, place graph    │          │ (availability, cached    │
                 │ (edges, route lines,        │          │  for 10 minutes)         │
                 │ heights), availability cache│          └──────────────────────────┘
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
| Prices, season dates, booking method, missing details | rare, no API | Read from the hut websites by a language model once or twice per season (`scrape_huts`, §3.7); manual entry for corrections |
| Bed availability | constant | One request per hut returns the whole season. Cached for 10 minutes and used as a search filter |
| Hut↔hut and stop↔hut connections | almost never | Precomputed offline on the swissTLM3D trail graph (rebuilt yearly with each new edition) |

**Key principles**
- Keep **source data separate from manual edits** (`hut_override`). The app reads "override if present, else source" (view `hut_effective`).
- Store the **raw API JSON** so fields can be re-parsed without re-fetching.
- Store prices as **components**, not totals. Compute estimates per request.
- Tour suggestions and price estimates are **computed, not stored** (until saved tours exist).
- **Offline build and app are separate.** The full trail graph (~389k nodes) only exists inside `build_edges`, which runs in its own process (on the server: a one-off container, `server/refresh.sh`). The app only reads the small place graph (huts + stops) and simplified route lines from SQLite.
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
- **Availability in rounds:** search, fetch the season calendars of the huts in the best 25 candidates (cached, max 40 fetches), search again with them, up to 3 rounds. A round with nothing left to fetch is the result (no extra pass). Tours further down the list are checked when opened.
- **Hut usable on a date:** prefer hut-reservation.org's daily status (SERVICED = catered, UNSERVICED = self-service only, CLOSED) and free beds ≥ group size. Otherwise fall back to the SAC monthly codes (`catering`, `self_service`). `1` (partly) means "check dates", and huts with no data are "unknown": allowed but ranked lower and flagged.
- Beam search over **hut sequences**: N huts (one per night). Public transport is attached, not searched (since 21 Oct 2026): the first hut needs a stop to get there and the last hut one to get home. By default the **closest** (walking time) that is **served on that day** and whose connection fits (with the first connection you reach the hut by 18:00; with the last one you needn't leave the hut before 08:00), otherwise the closest served one, flagged as a tight connection. Optional: avoid mountain lifts (stops served only by cable cars, chairlifts or funiculars). Days between huts must fall within [min, max] hours; the days to the first and from the last hut only have the maximum. The tour view lists up to 6 other stops per end (closest first, with their walk, service that day and the tour's score with them) to switch to. The catering preference must match on each night's date.
- Ranking (`app/planner/ranking.py`): each tour gets sub-scores 0–1: new trail (share not walked twice), shape (traverse or loop, not out-and-back or zig-zag), confirmed free beds, opening-data certainty, even days, easy trails, hut altitude, short approach, day-length fit. "Best" is their weighted average with **fixed weights from `huthop.toml`** (not changeable in the UI; day-length fit is weighted 0 since 19 Oct 2026). The About dialog lists the criteria and weights (`/api/info`).
- Route geometry (`app/planner/geometry.py`): each edge's route line becomes a set of 40 m grid cells; shared cells between a tour's days = trail walked twice. Retracing is also penalised **inside** the beam search, so such tours don't crowd out better ones. Shape uses 8 sample points per edge. Cells and samples are computed by `build_edges` and stored packed on the edge (~12 MB in all); a 1 km coarse check skips pairs of edges that can't overlap.
- The beam search (width 1000) keeps the partial tours with the lowest cost, which is what each day loses of "best" (same weights: trail difficulty, short first/last day, retracing, even days, and per night opening data, free beds and altitude; shape only at the end). It keeps at most 3 trail-limit variants per hut sequence, so the beam holds many different hut sequences. "Even days" only compares the days between huts (the first and last day count for "short approach"). The complete tours are ranked by "best" and diversified into a **pool of up to 40** (huts shared with an earlier tour < 60 %, a hut in at most 3 tours unless fewer than 10 remain). The browser sorts the pool (best, shortest, least ascent, highest huts, most free beds) without a new search. Tours whose free beds weren't checked yet say so on their card ("Beds: open to check") and are checked when opened.

### Data model (SQLite)

The schema lives in `app/db/schema.sql` (applied on startup; added columns are migrated in `app/db/__init__.py`).
All coordinates are stored in LV95 and WGS84.

| Table / view | Holds |
|---|---|
| `place` | Everything a tour can start, end or stay at: huts and stops, with LV95 + WGS84, altitude, snap distance to the trail network |
| `hut` | SAC (or hand-added) hut data: SAC and hut-reservation.org ids, beds, phone, services, photos, free access routes, raw API JSON, `source` |
| `hut_month` | SAC opening codes per month: `catering` (staffed) and `self_service` (winter room), 0 yes / 1 partly / 2 no; all 2 = no data |
| `hut_override` | Manual corrections per hut and field (`beds`, `catering.7`, …), never touched by syncs |
| `hut_effective`, `hut_month_effective` | Views: override if present, else source. The app reads these |
| `hut_web` | What the hut website says (§3.7): status (ok / unreachable / no_info), phone, booking methods and page, winter room, practical notes, when and by which model it was read |
| `hut_season` | Opening periods from the hut website: catered / self-service / exceptional closure, MM-DD to MM-DD, year, vague flag, source quote and whether it was found word for word. Refine the SAC months to days (`app/planner/opening.py`) |
| `hut_price` | Price components per person and night from the hut website (overnight, half board, breakfast, dinner, tax, winter room, kitchen use), member / regular / youth / child, with label, year, source quote |
| `stop` | Public transport stops: official number, transport modes |
| `edge` | Directed walking connections per trail-category limit: time, distance, ascent/descent, hardest category, matching SAC route, route line (encoded polyline), heights per point, `excluded` + `note` (manual, kept across rebuilds) |
| `availability`, `availability_fetch` | Cached hut-reservation.org calendars (per hut and day) and when/whether each hut was fetched |
| `data_version` | Counter bumped by triggers on every change to huts, overrides, stops and edges; the app reloads its cached graph when it changes |

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
- `tel` and `email` are shown in the app (private, invited users only; decided 2026-10-21) as call and mail buttons, kept as `hut.phone` / `hut.email` (website values as fallback). Don't republish them.

### 3.2 hut-reservation.org (availability, bookings)

- Used by most SAC huts (and DAV/ÖAV) for reservations.
- From the [domwoe/hut-reservation-mcp](https://github.com/domwoe/hut-reservation-mcp) README:
  - availability lookups are unauthenticated
  - the hut list requires login
  - booking endpoints (`preBook`, `submit`, `myReservations`, …) exist but are undocumented
- Availability: `GET /api/v1/reservation/getHutAvailability?hutId=X&step=WIZARD` (no login, no CSRF) returns the whole booking window per day: `freeBeds`, `totalSleepingPlaces`, `freeBedsPerCategory`, `hutStatus` (SERVICED / UNSERVICED / CLOSED). `0/0` = dates not released yet, not "full".
- Hut info: `GET /api/v1/reservation/hutInfo/{id}` (beds, categories, website). **No prices** in public responses. Details: `spikes/hut_reservation_findings.md`.
- Booking is out of scope. Only read availability. (A booking helper that opens the booking pages is on the "Later" list.)

### 3.3 Routing: swissTLM3D hiking network

- **swissTLM3D Wanderwege** (STAC `ch.swisstopo.swisstlm3d-wanderwege`, GeoPackage, ~200 MB, open data): the official signposted network as 3D lines, with `wanderwege` = Wanderweg / Bergwanderweg / Alpinwanderweg. Builds one connected graph (389k nodes) in about 45 s with pyogrio + shapely (networkx in the spike, SciPy in `build_edges`).
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
- Timetable (lines per stop, summer departures): GTFS from opentransportdata.swiss, `timetable-{year}-gtfs2020/permalink` (redirects to the current weekly file, ~90 MB). `stops.txt` has a `didok` column = the stop number above. Lines = `route_id` without the year suffix (`92-121-A-j27-1` → `92-121-A`). This year's and (from autumn) next year's timetable are both loaded, so any date up to the end of next season can be planned; days before today are dropped. Per stop and service pattern: first and last time and number of calls, plus the days each pattern runs (bit mask), so the planner knows whether a stop is served on a date (`app/planner/transit.py`). See `app/ingest/load_timetable.py`.
- Used as tour start/end nodes. Includes cable car, gondola and lift stations, which are key for hut tours (start or end at a top station). GeoPackage layer `Betriebspunkt`: `Nummer` (stop number), `Betriebspunkttyp_Bezeichnung` (keep `Haltestelle*`), `Verkehrsmittel_Code` (A bus, B train, C tram, D metro, E rack railway, F funicular, G cable car, H chairlift, I boat, J elevator).

### 3.6 Coordinate conversion

- SAC, swissTLM3D and the stop list all use LV95 (EPSG:2056). Store LV95 and convert to WGS84 (EPSG:4326) for the map with `pyproj`.

### 3.7 Hut websites (scraped, read by a language model)

- 380 of 384 huts have a website in the SAC data; 165 are not on hut-reservation.org and 65 of those have no SAC opening data either. Their websites often give season dates, prices and how to book.
- `app/ingest/scrape_huts.py`, three stages: **fetch** (start page plus up to 6 linked pages of the same site that look like prices / opening / booking, and up to 3 price-list PDFs; robots.txt respected, one request per 2 s per host; `data/raw/web/`), **extract** (Gemini 3.5 Flash on Vertex AI through the gcloud login, `app/clients/gemini.py`; fixed JSON schema, temperature 0, every value with its source sentence), **load** (into `hut_web`, `hut_season`, `hut_price`; values that don't parse are dropped; a quote not found word for word in the page text is stored as unverified and shown with "?").
- Pilot (20 private huts, Oct 2026): 18 read, 2 dead links; 60 of 73 quotes found word for word, the rest mostly rebuilt from price tables. Exceptional closures ("closed in summer 2026 after a landslide") come out as `closed` seasons. Gemini sometimes writes NUL for an accented letter; stripped on load.
- Full run (21 Oct 2026, ~70 min fetch + ~70 min extraction): 362 of 380 sites fetched (7 refuse bots with 403, the rest dead or broken), 348 about the hut; 277 huts with season dates (52 of the 82 without SAC opening data), 21 exceptional closures (renovations, hunting season), 267 huts with prices (CHF median per night incl. half board and tax: 90, members 78). Restricted prices (residents, children, one section's members) are skipped in the estimate. Known gaps: some sites list only rooms or suites, not the dormitory.
- Runs on the Mac (gcloud). The server's database is the master copy, so copy `data/raw/web/*.json` (no PDFs needed) to the server and run `python -m app.ingest.scrape_huts load` there (`docs/deployment.md`).

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

- [x] Tour search (beam search, width 3000; since 21 Oct 2026 width 1000 with variants per hut sequence and a cost aligned with "best": 2-night search ~0.05 s warm locally, was ~0.3 s) with date-aware catering/self-service checks (SAC monthly codes). All edge variants up to the trail limit are alternatives, so a harder limit only adds options; a small per-day penalty prefers easier trails between equal tours.
- [x] `POST /api/tours/suggest` and `GET /api/stops` (start picker). Inputs: date (not in the past), nights, hours per day, hardest trail, catering, map area, start near a stop (1.5 km), back to start (3 km), include unknown huts.
- [x] "Plan a tour" tab: form, result cards with day bars, tour detail timeline, route on the map with numbered huts; state in the URL.

### Phase 4: Prices + availability

- [x] Availability: season calendar per hut from hut-reservation.org, **cached 10 minutes** (availability changes quickly). Tour search runs in up to 3 rounds: search, fetch the calendars of huts in the best 25 candidates (max 40 fetches, 2 in parallel), search again. Full or closed nights drop out; free-bed nights use the daily status (SERVICED = catered, UNSERVICED = self-service); not-yet-released (0 of 0) and huts without online booking fall back to SAC monthly codes and rank slightly lower. Typical search: 3–6 s with fresh fetches.
- [x] Planner: number of people, "check free beds" option, card badge (beds free / N nights unconfirmed), free beds + "Book" link per night.
- [x] A CLOSED 0-of-0 night before the hut's released season counts as closed (e.g. Chamanna Cluozza before 3 June), not "not bookable yet". Only nights with nothing released later stay "not bookable yet".
- [x] Prices, season dates, booking method and notes from the hut websites (§3.7), shown in the hut detail with source and date. Season dates refine the planner's nights to days, and fill in huts without SAC opening data.
- [x] Phone number and email (SAC, else website) as call / mail buttons; editable as overrides.
- [x] Cost estimate per tour (`app/planner/costs.py`): per night the cheapest bed, half board when catered, winter room + kitchen fee when self-service, plus tourist tax; member or regular prices (switch in the form, no new search), total for the group, "from" when some nights have no prices, EUR summed separately. Card chip, "Cheapest" sort, per-night line in the tour detail.

### Later

- ~~GPX export~~ done: `GET /api/tours/{id}/gpx`, one track per day + waypoints. Tour ids are `date_places_limits` (e.g. `2027-07-17_123-45-67-89_2-2-3`), stable across edge rebuilds, so they also serve sharing, saved tours and the editor.
- Non-SAC huts, better ranking.
- ~~Offline use (PWA) and saved tours~~ done (Nov 2026, issue #5): installable (`web/manifest.json`, icons) with a hand-written service worker (`web/sw.js`). Our files and API network first, the cache only as a fallback, so a deploy is never hidden; the app shell, `/api/huts`, `/api/osm-paths` and `/api/info` are kept with it (a versioned cache; `skipWaiting` and `clients.claim`). Only 200 same-origin responses of the expected content type are cached, never a Cloudflare Access redirect. A tour in "My tours" is saved when added or opened (`web/js/offline.js`): its JSON (with the height profiles), its huts' details and the topographic map along the route (one tile around each point, zoom 8 up to the level that still fits in 500 tiles, 4 downloads at a time: swisstopo fair use), in a cache per tour that is deleted with the tour. Offline, the app opens on the topographic map (the light map's vector tiles aren't saved) and says "Offline — showing saved data from <date>". Saved tours stay per device (local storage): no accounts or sync.
- ~~Elevation in GPX~~ done: `edge.elevation` holds the swissTLM3D height of each route point (from `build_edges`); used for `<ele>` in GPX and a height profile per day in the tour view.
- ~~Tour links keep the search hours~~ done: tour ids end in the hours per day (`…_2-2-3_3-6.5`), so a shared or reopened tour is scored as in its search; older ids default to 3–6 h.
- ~~Free-beds calendar~~ done: `GET /api/huts/{id}/availability?people=N`, a month calendar in the hut details (same 10-minute cache as tour checks).
- **Export to Komoot and the swisstopo app.** Neither seems to have a public import API (Komoot's API is for partners only; no documented import link for the swisstopo app; check both before building), so both go through GPX files. Komoot: one GPX per day (it imports one tour per file), then its upload page (komoot.com/upload). swisstopo app: on a phone, "share" the GPX to the app, which imports it as a track. map.geo.admin.ch can show a GPX by URL only if the file is publicly reachable, which HutHop isn't (tailnet only). Needs elevation in the GPX, so both apps show correct ascent.
- ~~Custom sorting and a "best" score~~ done (see Tour search); weights fixed in `huthop.toml`.
- ~~General settings~~ done (21 Oct 2026): ⚙ in the header, kept in the browser (`web/js/settings.js`) and sent with every request that offers or shows routes. Hardest trail (default blue-white), highest point of a route (default no limit; `edge.max_altitude_m` from the height profile), avoid mountain lifts at the start and end, walking pace (times scale with it; the search divides your hours by it), Alpine Club member prices (default on), check free beds, metric or imperial units. Routes outside them are never offered (search, editor, a hut's connections); a tour made earlier or shared opens with a note ("Outside your settings: …"). The search form keeps hours, huts, end and huts without opening data.
- **Tour editor** (agreed 21 Oct 2026, replaces the "Other stops" list): edit any tour, or start one from a hut. Each day is a stored edge and a tour id names the whole tour, so an edit = another tour id, rebuilt with `build_tour` (which keeps nights that no longer fit, flagged).
  - *Start:* any tour (search result or "Your tours") opens in edit mode; or "Plan a tour from here" in a hut's details: one night at that hut, with the closest stops that run on the planner's date and the planner's hours. No building from nothing.
  - *Edits:* E1 start or end stop (stops connected to the first/last hut: walk, transport type, served that day, first/last connection, time flags); E2 swap a hut (huts connected to both neighbours); E3 add a night between two huts or at either end (at an end, the stop is re-picked: closest that runs that day); E4 remove a night (if the neighbours connect directly); E5 change the start date (all nights move; opening, free beds and stops re-checked); E6 a day's trail limit (where another variant of that day's edge exists).
  - *After each edit:* walking times, score and sub-scores, opening per night and stop service recomputed, with the change shown (e.g. score 82 % → 79 %). A tour you design needn't follow the search settings: days outside the hours or on harder trails are neither blocked nor flagged (warnings were tried and removed, 21 Oct 2026); the night's opening status and the start / end connection times are still shown. Free beds re-checked automatically for new huts when the search had "check free beds" on. Undo and redo.
  - *Candidates:* per slot, huts and stops **nearest first** (the walk between the new hut and its neighbouring huts in the tour, or to / from a stop), each with that distance and walking time, the tour's day lengths, ascent and the night's status; no scores in the editor; also shown on the map, where a click on a hut offers the edits it fits (swap night N, add between …). On phones, the lists only.
  - *Your tours:* an edited tour becomes a new entry (the original stays as it was), and any tour can be added. Kept in the browser (local storage, per device), each entry as its tour id; the open tour's id is in the URL, so it can be shared. Stays per device (decided Nov 2026: no accounts or sync); saved for offline use too.
  - *Plan:* (1) backend: an options endpoint listing every slot's candidates with their resulting tour id and key numbers, flags in the tour JSON, a tour from a hut; tests. (2) "Your tours" and the tour id in the URL. (3) Edit mode in the tour view (`web/js/tour-editor.js`; `editor.js` is the hut-data editor): slot menus, candidate lists and map candidates, undo/redo, free beds; "Other stops" and its endpoint removed. (4) "Plan a tour from here" in the hut details. (5) Docs, deploy.
- SAC times as automatic overrides (per-grade calibration for T4+).
- ~~Fewer redundant stops in a hut's connections~~ done: `load_timetable` reads the lines per stop from the GTFS timetable; `build_edges` keeps per hut and direction only the closest stop of each line with summer service (stops without summer service get no edges). Stop edges −30 %, e.g. Glecksteinhütte: 10 Grindelwald stops instead of ~25. The connection list shows lines and departures per day. Possible next step: also drop stops whose lines can all be reached by riding from a closer kept stop (fewer town-bus stops, but fewer traverse end points too).
- **Book all huts of a tour together:** one "Book this tour" step instead of a "Book" link per night. Two levels:
  1. ~~*Booking helper (small, safe)*~~ done (23 Oct 2026, `web/js/booking.js`): "Book this tour" in the tour view opens a checklist of the nights (it replaces the "Book" link per night). "Open next" opens the next unbooked night's hut-reservation.org page; you choose the dates there (whether the link can prefill them is still to check) and tick the night off. Ticks are kept in the browser per hut and night, so they stay with an edited tour, and booked nights show "✓ Booked" in the timeline. Opening it checks free beds again with calendars at most a minute old (`GET /api/tours/{id}?fresh=true`, `availability.FRESH_TTL_S`); "Check free beds again" repeats that. Huts not on hut-reservation.org show what their website says about booking, its booking page, website, email and phone.
  2. *Real booking through hut-reservation.org (large, risky):* its booking endpoints (`preBook`, `submit`, …) are undocumented and need the user's own login, and the site's terms likely don't allow automated bookings. That would mean storing credentials and changing §3.2's "booking is out of scope". Only with the operator's permission; otherwise stay with level 1.
- **Public transport within a tour:** allow a day to include a bus/train/cable car ride, e.g. walk down from a hut, take the bus through the valley, walk up to the next hut. Approach: combine a hut→stop edge, a timetable ride (or a simple stop→stop transfer table from the official timetable data), and a stop→hut edge into one day, counting only the walking time against the daily limit. Needs timetable data (e.g. opentransportdata.swiss GTFS) and a cap on rides per day.
- ~~Warnings for official trail closures and military shooting zones~~ done (27 Oct 2026): `GET /api/tours/{id}/warnings` asks geo.admin identify (`ch.astra.wanderland-sperrungen_umleitungen`, `ch.vbs.schiessanzeigen`) for features within 20 m of each day's route line (in chunks of 100 points; whole lines exceed the URL limit), cached an hour per line in the process. The tour view shows closures and crossed danger zones per day, with the shooting times on that day (shooting notices are published about two weeks ahead).
- ~~Short-term weather~~ done (27 Oct 2026): MeteoSwiss ICON-CH2 through Open-Meteo (`models=meteoswiss_icon_ch2`, about 5 days, corrected to the hut's altitude; free for non-commercial use), `GET /api/huts/{id}/weather`, cached an hour in the process. A row of days in the hut details; in the tour view a line per day at that day's hut (the last day: the hut it starts at). MeteoSwiss's own open data was skipped: large CSV files for fixed points, not any spot.
- **Public transport stops with timetables:** add a stop and show its timetable (departures per line on the planned date, from the loaded GTFS data).
- ~~Favourite huts~~ done (27 Oct 2026, `web/js/favourites.js`): "☆ Favourite" in the hut details, a ★ before the name in the list, and a "★ Favourites" filter chip; kept in the browser like "Your tours".
- **Mobile testing:** the app has a phone layout (bottom sheet below 720 px) but no frontend tests at all, and it is mostly checked on a desktop. Two parts:
  1. *Manual checklist* on a real iPhone and Android phone on the live site: map gestures (pinch, pan, tap on huts), bottom sheet, filter panel, tour form and results, tour detail with height profiles, free-beds calendar, GPX download/share to the swisstopo app, editor dialogs, landscape orientation, dark mode.
  2. ~~*Automated browser tests*~~ done (`tests/test_mobile.py`, issue #11): Playwright (Python, `pytest-playwright`) in mobile emulation (e.g. iPhone 13 and Pixel 7 viewports, touch enabled) against the app on a small test database: no horizontal scrolling, key flows work (select a hut, open connections, search tours, open a tour), no console errors. Run in CI next to pytest; screenshots of failures as build artifacts.
- **Winter tours (ski touring, snowshoe hiking):** the same planner for winter, with a season switch. What changes:
  - *Routes:* winter routes don't follow the summer hiking network. Candidates: swisstopo's ski touring and snowshoe route layers (geo.admin, `ch.swisstopo-karto.skitouren`, `ch.swisstopo-karto.schneeschuhrouten`; check license and whether the geometry can be downloaded) and SAC's ski touring and snowshoe routes (only routes with `availability == "free"`, see §6). Probably a separate winter edge set (`season` column on `edge`) with its own build.
  - *Times and grades:* the Swiss hiking-time formula doesn't fit skis (fast descents) or snowshoes. Use SAC times where given, otherwise a separate formula per discipline. Grades: SAC ski scale (L, WS, ZS, …) and snowshoe scale (WT1–WT6) instead of trail colours.
  - *Huts:* winter opening (SAC monthly codes, catered in winter vs. winter room only) and winter free beds from hut-reservation.org (same API).
  - *Public transport:* `load_timetable` averages departures over July and August; winter needs a second window (e.g. January to March), otherwise winter-only ski buses and lifts get no connections (they are dropped today).
  - *Safety:* a link to the SLF avalanche bulletin per tour and wildlife rest areas (`ch.bafu.wrz-wildruhezonen`) on the map. No avalanche risk assessment by the app; tours are planning aids only.
- ~~Docker Compose for a home server~~ done (server2, since 26 Oct the Oracle VM; see `docs/deployment.md`).

---

## 5. Stack and repo layout

- **Backend:** Python 3.12, FastAPI, `httpx`, `pyproj`, plain `sqlite3` (WAL mode)
- **Routing:** own graph on swissTLM3D (pyogrio, shapely, SciPy sparse Dijkstra; networkx only in the spikes)
- **Frontend:** plain HTML/JS ES modules + MapLibre GL, no build step
- **Ops:** `uv run` locally; Docker image built by GitHub Actions, Docker Compose on the Oracle VM

The repo layout is described in `README.md`.

---

## 6. Constraints, risks and legal notes

**Legal / terms (not legal advice):**
- SAC's [legal information](https://www.sac-cas.ch/en/meta/impressum/legal-information/): site content is copyrighted and "only freely accessible for browsing purposes". Public or commercial use requires SAC's **prior written consent**. Reproduction requires naming sac-cas.ch as the source.
- So: **private use only**, credit SAC as the source, and no redistribution of their data.
- SAC has granted API permission before (e.g. the OSM hut-metadata import, with the condition "moderate usage"). If the project ever goes public, **ask SAC first**.
- Paid route portal content (routes with `availability != "free"`) must not be used.
- hut-reservation.org has its own terms. Read only, low rate, no booking automation.
- Hut websites: public pages only, robots.txt respected, low rate, once or twice per season. Extracted facts (dates, prices) for private use; no copies of the pages are published.

**Technical risks:**
- Undocumented APIs can change or disappear. Store raw JSON and keep the clients isolated.
- Computed times are less reliable for T4+ (official times there are hand-adjusted). Show the SAC time next to the computed one where available.
- Huts far from the signposted network (long snaps) can get wrong or missing edges. Review snaps over ~200 m.
- The monthly SAC codes are coarse. Prefer hut-reservation.org's daily status where it exists.
- Price and season data from the websites goes stale and can be misread by the model. Show source and date with every value; re-run before each season.
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
6. ~~Tour ranking criteria beyond hours per day and price~~ → "best" sub-scores (§2 Tour search). Open: tune the weights after real use.
