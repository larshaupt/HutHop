# HutHop

A private web app to browse Swiss mountain huts (SAC plus selected non-SAC huts) and to suggest multi-day summer hut tours from your constraints, with availability checks and price estimates.

**Status:** hut map, walking connections, multi-day tour suggestions with live free-bed checks (hut-reservation.org, cached 10 min). Prices postponed. Phase 0 findings are in [`spikes/`](spikes/). See [`hut-tour-planner.md`](hut-tour-planner.md) for the full project brief: requirements, system design, data model, known APIs and the development plan.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync                                   # create .venv and install dependencies
uv run python -m app.ingest.refresh       # huts, stops, trails (if new) and route graph (if needed)
uv run uvicorn app.main:app --reload      # http://localhost:8000
uv run pytest                             # tests
```

`refresh` runs the individual jobs (`sync_sac`, `load_stops`, `build_edges`), downloads a new swissTLM3D edition
only when swisstopo publishes one, and rebuilds the route graph only when hut/stop positions or trails changed
(`--rebuild` forces it). Each run is logged to `data/refresh_log.json`. Run it weekly during the season.
`sync_sac --from-file data/raw/sac/huts.json` re-imports the last saved API response without fetching.

Deployment (server2, tailnet only): see `docs/deployment.md`. Changing data needs the admin password once one is
set (`python -m app.admin set-password`); on the server, changes are refused until then.

Settings: `huthop.toml` (weights of the "best" tour order). Manual corrections: "Edit hut data" in a hut's detail
view, "+ Add hut" in the hut list; they survive re-syncs. New huts get walking connections at the next
`refresh --rebuild`.

`build_edges` needs the swissTLM3D hiking network (swisstopo open data, about 200 MB) unpacked in `data/tlm/`:
<https://data.geo.admin.ch/ch.swisstopo.swisstlm3d-wanderwege/swisstlm3d-wanderwege/swisstlm3d-wanderwege_2056_5728.gpkg.zip>. Manual `excluded` flags on edges survive rebuilds.

## Layout

```
app/
  api/       FastAPI routes (/api/huts, /api/huts/{id}/edges, /api/huts/{id}/overrides, /api/edges/{id},
             /api/tours/suggest, /api/tours/{id}, /api/tours/{id}/gpx, /api/stops)
  db/        schema.sql and connection
  ingest/    offline jobs (refresh, sync_sac, load_stops, build_edges)
  planner/   tour search, ranking, opening rules, availability cache, GPX, hiking time, polyline
  clients/   external APIs (SAC, hut-reservation.org)
web/         static frontend (MapLibre + swisstopo tiles, no build step), ES modules in web/js/:
             main (wiring), state (URL hash), map, huts, connections, tours, editor, util
spikes/      Phase 0 experiments and findings
tests/
```

## Usage and data

Private, non-commercial use only. Hut data comes from the SAC (© sac-cas.ch) and must not be redistributed. Maps and trail data © swisstopo. Suggested tours are planning aids: always check difficulty, conditions, weather and official sources before you go.
