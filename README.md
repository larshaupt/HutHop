# HutHop

A private web app to browse Swiss mountain huts (SAC plus selected non-SAC huts) and to suggest multi-day summer hut tours from your constraints, with availability checks and price estimates.

**Status:** hut map, walking connections, multi-day tour suggestions with live free-bed checks (hut-reservation.org, cached 10 min). Prices postponed. Phase 0 findings are in [`spikes/`](spikes/). See [`hut-tour-planner.md`](hut-tour-planner.md) for the full project brief: requirements, system design, data model, known APIs and the development plan.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync                                   # create .venv and install dependencies
uv run python -m app.ingest.sync_sac      # fetch SAC huts into data/huthop.db (~10 s, 5 requests)
uv run python -m app.ingest.load_stops    # public transport stops (~5 MB download)
uv run python -m app.ingest.build_edges   # route graph on swissTLM3D (~90 s, ~2 GB RAM)
uv run uvicorn app.main:app --reload      # http://localhost:8000
uv run pytest                             # tests
```

`sync_sac --from-file data/raw/sac/huts.json` re-imports the last saved API response without fetching.

`build_edges` needs the swissTLM3D hiking network (swisstopo open data, about 200 MB) unpacked in `data/tlm/`:
<https://data.geo.admin.ch/ch.swisstopo.swisstlm3d-wanderwege/swisstlm3d-wanderwege/swisstlm3d-wanderwege_2056_5728.gpkg.zip>. Manual `excluded` flags on edges survive rebuilds.

## Layout

```
app/
  api/       FastAPI routes (/api/huts, /api/huts/{id}/edges, /api/edges/{id}, /api/tours/suggest, /api/stops)
  db/        schema.sql and connection
  ingest/    offline jobs (sync_sac, load_stops, build_edges)
  planner/   tour search, availability cache, hiking time (Swiss formula), polyline encoding
  clients/   external APIs (SAC, hut-reservation.org)
web/         static frontend (MapLibre + swisstopo tiles, no build step): app.js (map, huts), tours.js (planner)
spikes/      Phase 0 experiments and findings
tests/
```

## Usage and data

Private, non-commercial use only. Hut data comes from the SAC (© sac-cas.ch) and must not be redistributed. Maps and trail data © swisstopo. Suggested tours are planning aids: always check difficulty, conditions, weather and official sources before you go.
