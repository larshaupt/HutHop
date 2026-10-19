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

CREATE TABLE IF NOT EXISTS hut_price (
  hut_id     INTEGER REFERENCES hut(place_id),
  year       INTEGER NOT NULL,
  item       TEXT NOT NULL CHECK (item IN
               ('overnight','half_board','breakfast','tax','self_catering_fee')),
  member     REAL,                    -- CHF
  non_member REAL,
  youth      REAL,
  source     TEXT NOT NULL,           -- 'manual' | 'scraped' | 'hrs'
  source_url TEXT,
  updated_at TEXT,
  PRIMARY KEY (hut_id, year, item)
);

-- Manual corrections that survive re-syncs. Fields: 'name', 'altitude', 'beds', 'shelter_beds', 'hrs_id',
-- 'website', 'catering.<month>', 'self_service.<month>'. See app/api/manual.py.
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

-- Source data merged with manual overrides: "override if present, else source".
DROP VIEW IF EXISTS hut_effective;
CREATE VIEW hut_effective AS
SELECT
  p.id, p.name AS source_name,
  COALESCE((SELECT json_extract(o.value, '$') FROM hut_override o WHERE o.hut_id = p.id AND o.field = 'name'), p.name) AS name,
  p.lat, p.lon, p.e_lv95, p.n_lv95, p.snap_m,
  COALESCE((SELECT json_extract(o.value, '$') FROM hut_override o WHERE o.hut_id = p.id AND o.field = 'altitude'), p.altitude_m) AS altitude_m,
  h.sac_id, h.operator, h.owner, h.description, h.services, h.photos, h.routes, h.source, h.synced_at,
  COALESCE((SELECT json_extract(o.value, '$') FROM hut_override o WHERE o.hut_id = p.id AND o.field = 'hrs_id'), h.hrs_id) AS hrs_id,
  COALESCE((SELECT json_extract(o.value, '$') FROM hut_override o WHERE o.hut_id = p.id AND o.field = 'beds'), h.beds) AS beds,
  COALESCE((SELECT json_extract(o.value, '$') FROM hut_override o WHERE o.hut_id = p.id AND o.field = 'shelter_beds'), h.shelter_beds) AS shelter_beds,
  COALESCE((SELECT json_extract(o.value, '$') FROM hut_override o WHERE o.hut_id = p.id AND o.field = 'website'), h.website) AS website
FROM place p JOIN hut h ON h.place_id = p.id;

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
CREATE TRIGGER IF NOT EXISTS stop_ins AFTER INSERT ON stop BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS stop_upd AFTER UPDATE ON stop BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS stop_del AFTER DELETE ON stop BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS edge_ins AFTER INSERT ON edge BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS edge_upd AFTER UPDATE ON edge BEGIN UPDATE data_version SET version = version + 1; END;
CREATE TRIGGER IF NOT EXISTS edge_del AFTER DELETE ON edge BEGIN UPDATE data_version SET version = version + 1; END;
