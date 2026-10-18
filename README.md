# HutHop

A private web app to browse Swiss mountain huts (SAC plus selected non-SAC huts) and to suggest multi-day summer hut tours from your constraints, with availability checks and price estimates.

**Status:** planning / Phase 0 (spikes). See [`hut-tour-planner.md`](hut-tour-planner.md) for the full project brief: requirements, system design, data model, known APIs and the development plan.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync          # create .venv and install dependencies
uv run python …  # run scripts inside the environment
```

GraphHopper (routing) runs in Docker; see the brief, §3.3.

## Usage and data

Private, non-commercial use only. Hut data comes from the SAC (© sac-cas.ch) and must not be redistributed. Suggested tours are planning aids: always check difficulty, conditions, weather and official sources before you go.
