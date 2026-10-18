# HutHop

A private web app to browse Swiss mountain huts (SAC plus selected non-SAC huts) and to suggest multi-day summer hut tours from your constraints, with availability checks and price estimates.

**Status:** Phase 1 done: a filterable map of all 384 SAC-listed huts. Phase 0 findings are in [`spikes/`](spikes/). See [`hut-tour-planner.md`](hut-tour-planner.md) for the full project brief: requirements, system design, data model, known APIs and the development plan.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync                                   # create .venv and install dependencies
uv run python -m app.ingest.sync_sac      # fetch SAC huts into data/huthop.db (~10 s, 5 requests)
uv run uvicorn app.main:app --reload      # http://localhost:8000
uv run pytest                             # tests
```

`sync_sac --from-file data/raw/sac/huts.json` re-imports the last saved API response without fetching.

Routing data (Phase 2): the swissTLM3D hiking network (swisstopo open data, about 200 MB) goes in `data/tlm/`; see the brief, §3.3.

## Layout

```
app/
  api/       FastAPI routes (/api/huts)
  db/        schema.sql and connection
  ingest/    offline jobs (sync_sac)
  clients/   external APIs (SAC)
web/         static frontend (MapLibre + swisstopo tiles, no build step)
spikes/      Phase 0 experiments and findings
tests/
```

## Usage and data

Private, non-commercial use only. Hut data comes from the SAC (© sac-cas.ch) and must not be redistributed. Maps and trail data © swisstopo. Suggested tours are planning aids: always check difficulty, conditions, weather and official sources before you go.
