-- HutHop SQLite schema. See hut-tour-planner.md §2 for the design.
PRAGMA foreign_keys = ON;

-- Places: everything a tour can start, end or stay at
CREATE TABLE IF NOT EXISTS place (
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

CREATE TABLE IF NOT EXISTS hut (
  place_id      INTEGER PRIMARY KEY REFERENCES place(id),
  sac_id        INTEGER UNIQUE,       -- SAC portal POI id; NULL for manually added huts
  hrs_id        INTEGER,              -- hut-reservation.org id (NULL = none)
  operator      TEXT,                 -- 'sac' | 'private'
  owner         TEXT,                 -- owning section or person, as published by SAC
  beds          INTEGER,
  shelter_beds  INTEGER,              -- places in the winter room / emergency shelter
  website       TEXT,
  phone         TEXT,                 -- hut / reservation phone as published by SAC (`tel`)
  email         TEXT,                 -- hut / reservation email as published by SAC
  description   TEXT,
  services      TEXT,                 -- JSON object of booleans
  photos        TEXT,                 -- JSON list of {caption, copyright, small, medium, large}
  routes        TEXT,                 -- JSON list of free SAC access routes (paid content excluded)
  source        TEXT NOT NULL,        -- 'sac_api' | 'manual' | 'scraped'
  raw           TEXT,                 -- original API JSON
  synced_at     TEXT
);

CREATE TABLE IF NOT EXISTS hut_month (
  hut_id        INTEGER REFERENCES hut(place_id),
  month         INTEGER CHECK (month BETWEEN 1 AND 12),
  self_service  INTEGER,              -- SAC `opening`: winter room / self-service open, 0 yes / 1 partly / 2 no
  catering      INTEGER,              -- SAC `catering`: catered, 0 yes / 1 partly / 2 no
                                      -- all months 2/2 = no data (unknown), not "closed"
  PRIMARY KEY (hut_id, month)
);

-- Prices per person and night, from the hut website (app/ingest/scrape_huts.py) or entered by hand.
-- Replaced per hut and source on every load.
CREATE TABLE IF NOT EXISTS hut_price (
  hut_id     INTEGER NOT NULL REFERENCES hut(place_id),
  item       TEXT NOT NULL CHECK (item IN ('overnight','half_board','breakfast','dinner','tax','winter_room',
                                           'self_catering_fee')),
  label      TEXT,                    -- original wording, e.g. "Halbpension inkl. Übernachtung (Massenlager)"
  includes_overnight INTEGER NOT NULL DEFAULT 0,  -- half board price includes the bed
  member     REAL,                    -- Alpine club members
  non_member REAL,                    -- regular price (same as member if there is only one)
  youth      REAL,
  child      REAL,
  currency   TEXT NOT NULL DEFAULT 'CHF',
  year       INTEGER,                 -- season the price is for (NULL = not stated)
  source     TEXT NOT NULL,           -- 'scraped' | 'manual'
  source_url TEXT,
  quote      TEXT,                    -- sentence on the website the price comes from
  verified   INTEGER,                 -- 1: quote found word for word in the fetched pages, NULL: from a PDF
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS hut_price_hut ON hut_price(hut_id);

-- Opening periods from the hut website. They refine the SAC monthly codes to days and fill in huts
-- without SAC opening data (app/planner/opening.py). Replaced per hut on every load.
CREATE TABLE IF NOT EXISTS hut_season (
  hut_id      INTEGER NOT NULL REFERENCES hut(place_id),
  kind        TEXT NOT NULL CHECK (kind IN ('catered','self_service','closed')),
                                      -- closed: exceptional closure (renovation, damage), always with year
  start_md    TEXT NOT NULL,          -- 'MM-DD'
  end_md      TEXT NOT NULL,          -- 'MM-DD' (inclusive; earlier than start_md = over New Year)
  year        INTEGER,                -- year the dates were published for (NULL = every year)
  approximate INTEGER NOT NULL DEFAULT 0,   -- vague dates ("mid June", "depending on snow")
  note        TEXT,
  source      TEXT NOT NULL,          -- 'scraped'
  source_url  TEXT,
  quote       TEXT,
  verified    INTEGER                 -- 1: quote found word for word in the fetched pages, NULL: from a PDF
);
CREATE INDEX IF NOT EXISTS hut_season_hut ON hut_season(hut_id);

-- Everything else read from the hut website, one row per hut (app/ingest/scrape_huts.py).
CREATE TABLE IF NOT EXISTS hut_web (
  hut_id           INTEGER PRIMARY KEY REFERENCES hut(place_id),
  url              TEXT NOT NULL,     -- website that was read
  status           TEXT NOT NULL,     -- 'ok' | 'unreachable' (dead link, error) | 'no_info' (not about this hut)
  phone            TEXT,              -- phone number on the website
  email            TEXT,              -- reservation email on the website
  booking_methods  TEXT,              -- JSON list: hut_reservation_org, online_other, email, phone, not_needed
  booking_url      TEXT,
  closed_otherwise INTEGER,           -- 1: the website says the hut is closed outside its seasons
  winter_room      INTEGER,           -- 1 / 0 / NULL (not stated)
  winter_room_beds INTEGER,
  beds             INTEGER,
  notes            TEXT,              -- practical facts in English, written by the extraction model
  fetched_at       TEXT,
  extracted_at     TEXT,
  model            TEXT
);

-- Manual corrections that survive re-syncs. Fields: 'name', 'altitude', 'beds', 'shelter_beds', 'hrs_id',
-- 'website', 'phone', 'email', 'catering.<month>', 'self_service.<month>'. See app/api/manual.py.
CREATE TABLE IF NOT EXISTS hut_override (
  hut_id     INTEGER REFERENCES hut(place_id),
  field      TEXT NOT NULL,
  value      TEXT NOT NULL,           -- JSON value
  note       TEXT,
  updated_at TEXT,
  PRIMARY KEY (hut_id, field)
);

CREATE TABLE IF NOT EXISTS stop (
  place_id  INTEGER PRIMARY KEY REFERENCES place(id),
  uic       INTEGER UNIQUE,           -- official stop number (DiDok/UIC, e.g. 8507492)
  mode      TEXT                      -- comma list: bus, train, tram, metro, rack_railway, funicular,
                                      -- cableway, chairlift, boat, elevator
);

-- Lines serving each stop, from the official timetable (app/ingest/load_timetable.py). Replaced on every load.
CREATE TABLE IF NOT EXISTS stop_line (
  stop_place INTEGER NOT NULL REFERENCES place(id),
  line       TEXT NOT NULL,           -- timetable route id without the year, e.g. 92-121-A
  label      TEXT,                    -- e.g. "B 121", "R61"
  departures REAL NOT NULL,           -- calls per day, averaged over July and August (0: no summer service)
  PRIMARY KEY (stop_place, line)
);

-- Timetables per year (load_timetable.py): which days each service pattern runs, and per stop and pattern the
-- first and last time and the number of calls. Together: is a stop served on a date, and from when to when.
CREATE TABLE IF NOT EXISTS timetable (
  feed       INTEGER PRIMARY KEY,     -- timetable year, e.g. 2027 (valid mid-December 2026 to mid-December 2027)
  version    TEXT,
  start_date TEXT NOT NULL,
  end_date   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timetable_service (
  feed    INTEGER NOT NULL,
  service INTEGER NOT NULL,           -- numbered per feed
  days    BLOB NOT NULL,              -- bit i (little-endian) = runs on start_date + i days
  PRIMARY KEY (feed, service)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS stop_service (
  feed       INTEGER NOT NULL,
  stop_place INTEGER NOT NULL,
  service    INTEGER NOT NULL,
  first_min  INTEGER,                 -- first call of the day, minutes after midnight (can exceed 1440)
  last_min   INTEGER,                 -- last call of the day
  calls      INTEGER NOT NULL,
  PRIMARY KEY (feed, stop_place, service)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS edge (
  id             INTEGER PRIMARY KEY,
  from_place     INTEGER NOT NULL REFERENCES place(id),
  to_place       INTEGER NOT NULL REFERENCES place(id),
  distance_m     INTEGER,
  ascent_m       INTEGER,
  descent_m      INTEGER,
  time_min       INTEGER,             -- hiking time in this direction (Swiss formula)
  max_category   INTEGER NOT NULL,    -- 1 yellow, 2 red-white, 3 blue-white (hardest along path)
  category_limit INTEGER NOT NULL,    -- search limit this edge was found with (1..3)
  source         TEXT NOT NULL,       -- 'tlm_routed' | 'manual'
  sac_route_id   INTEGER,             -- matching SAC route, shown for reference
  sac_time_min   INTEGER,
  sac_grade      TEXT,
  geometry       TEXT,                -- simplified encoded polyline
  elevation      TEXT,                -- height (m) of each geometry point, encoded (polyline.encode_values)
  max_altitude_m INTEGER,             -- highest point of the route (m), from the height profile
  footprint      BLOB,                -- grid cells of the route line, packed (planner/geometry.py: pack)
  samples        BLOB,                -- evenly spaced points of the route line, for the tour shape (same)
  excluded       INTEGER DEFAULT 0,
  note           TEXT,
  computed_at    TEXT,
  UNIQUE (from_place, to_place, category_limit, source)
);
CREATE INDEX IF NOT EXISTS edge_from ON edge(from_place) WHERE excluded = 0;

CREATE TABLE IF NOT EXISTS availability (
  hut_id        INTEGER REFERENCES hut(place_id),
  date          TEXT,
  hut_status    TEXT,                 -- 'SERVICED' | 'UNSERVICED' | 'CLOSED'
  free_beds     INTEGER,
  total_places  INTEGER,              -- 0 with free_beds 0 = not released yet, not "full"
  fetched_at    TEXT,
  PRIMARY KEY (hut_id, date)
);

-- Last calendar fetch per hut (successful or not), for the short availability cache
CREATE TABLE IF NOT EXISTS availability_fetch (
  hut_id      INTEGER PRIMARY KEY REFERENCES hut(place_id),
  fetched_at  TEXT NOT NULL,
  ok          INTEGER NOT NULL,
  error       TEXT
);

-- Source data merged with manual overrides: "override if present, else source". An override can be null (a field
-- cleared by hand): json_type() tells it ('null') from no override (NULL), which COALESCE could not.
DROP VIEW IF EXISTS hut_effective;
CREATE VIEW hut_effective AS
SELECT
  p.id, p.name AS source_name,
  CASE WHEN json_type(ov.o, '$.name') IS NULL THEN p.name ELSE json_extract(ov.o, '$.name') END AS name,
  p.lat, p.lon, p.e_lv95, p.n_lv95, p.snap_m,
  CASE WHEN json_type(ov.o, '$.altitude') IS NULL THEN p.altitude_m ELSE json_extract(ov.o, '$.altitude') END AS altitude_m,
  h.sac_id, h.operator, h.owner, h.description, h.services, h.photos, h.routes, h.source, h.synced_at,
  CASE WHEN json_type(ov.o, '$.hrs_id') IS NULL THEN h.hrs_id ELSE json_extract(ov.o, '$.hrs_id') END AS hrs_id,
  CASE WHEN json_type(ov.o, '$.beds') IS NULL THEN h.beds ELSE json_extract(ov.o, '$.beds') END AS beds,
  CASE WHEN json_type(ov.o, '$.shelter_beds') IS NULL THEN h.shelter_beds ELSE json_extract(ov.o, '$.shelter_beds') END AS shelter_beds,
  CASE WHEN json_type(ov.o, '$.website') IS NULL THEN h.website ELSE json_extract(ov.o, '$.website') END AS website,
  CASE WHEN json_type(ov.o, '$.phone') IS NULL THEN COALESCE(h.phone, (SELECT w.phone FROM hut_web w WHERE w.hut_id = p.id))
       ELSE json_extract(ov.o, '$.phone') END AS phone,
  CASE WHEN json_type(ov.o, '$.email') IS NULL THEN COALESCE(h.email, (SELECT w.email FROM hut_web w WHERE w.hut_id = p.id))
       ELSE json_extract(ov.o, '$.email') END AS email
FROM place p JOIN hut h ON h.place_id = p.id
LEFT JOIN (SELECT hut_id, json_group_object(field, json(value)) AS o FROM hut_override GROUP BY hut_id) ov
  ON ov.hut_id = p.id;

DROP VIEW IF EXISTS hut_month_effective;
CREATE VIEW hut_month_effective AS
SELECT
  m.hut_id, m.month,
  COALESCE((SELECT json_extract(o.value, '$') FROM hut_override o
            WHERE o.hut_id = m.hut_id AND o.field = 'self_service.' || m.month), m.self_service) AS self_service,
  COALESCE((SELECT json_extract(o.value, '$') FROM hut_override o
            WHERE o.hut_id = m.hut_id AND o.field = 'catering.' || m.month), m.catering) AS catering
FROM hut_month m;

-- Counter bumped by every change to what the tour search loads, so the cached route graph
-- (app/planner/graph.py: cached_graph) notices changes from any process: the app, sync_sac, build_edges.
CREATE TABLE IF NOT EXISTS data_version (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  version  INTEGER NOT NULL
);
INSERT OR IGNORE INTO data_version VALUES (1, 0);
CREATE TRIGGER IF NOT EXISTS place_ins AFTER INSERT ON place BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS place_upd AFTER UPDATE ON place BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS place_del AFTER DELETE ON place BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_ins AFTER INSERT ON hut BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_upd AFTER UPDATE ON hut BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_del AFTER DELETE ON hut BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_month_ins AFTER INSERT ON hut_month BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_month_upd AFTER UPDATE ON hut_month BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_month_del AFTER DELETE ON hut_month BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_override_ins AFTER INSERT ON hut_override BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_override_upd AFTER UPDATE ON hut_override BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_override_del AFTER DELETE ON hut_override BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_season_ins AFTER INSERT ON hut_season BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS hut_season_del AFTER DELETE ON hut_season BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS stop_ins AFTER INSERT ON stop BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS stop_upd AFTER UPDATE ON stop BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS stop_del AFTER DELETE ON stop BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS edge_ins AFTER INSERT ON edge BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS edge_upd AFTER UPDATE ON edge BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS edge_del AFTER DELETE ON edge BEGIN UPDATE data_version SET version = version + 1; END;

-- Feedback sent from the app (app/api/feedback.py)
CREATE TABLE IF NOT EXISTS feedback (
  id          INTEGER PRIMARY KEY,
  created_at  TEXT NOT NULL,
  message     TEXT NOT NULL,
  page        TEXT,                   -- the app's URL (view, hut, tour) when it was sent
  email       TEXT                    -- the sender's login email, from Cloudflare Access
);

-- Every tour search (app/api/tours.py), to analyse what people look for and improve the results
CREATE TABLE IF NOT EXISTS search_log (
  id          INTEGER PRIMARY KEY,
  created_at  TEXT NOT NULL,
  query       TEXT NOT NULL,          -- the request as JSON (TourQuery)
  results     INTEGER NOT NULL,       -- tours returned
  best_tour   TEXT,                   -- id of the first ("best") tour
  duration_ms INTEGER NOT NULL
);
