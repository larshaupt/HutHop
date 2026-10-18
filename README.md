# HutHop

A private web app to browse Swiss mountain huts (SAC plus selected non-SAC huts) and to suggest multi-day summer hut tours from your constraints, with availability checks and price estimates.

**Status:** Phase 0 (spikes) done, see [`spikes/`](spikes/) for findings. Routing uses the official swissTLM3D hiking network. See [`hut-tour-planner.md`](hut-tour-planner.md) for the full project brief: requirements, system design, data model, known APIs and the development plan.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync          # create .venv and install dependencies
uv run python …  # run scripts inside the environment
```

Routing data: the swissTLM3D hiking network (swisstopo open data, about 200 MB) goes in `data/tlm/`; see the brief, §3.3.

## Usage and data

Private, non-commercial use only. Hut data comes from the SAC (© sac-cas.ch) and must not be redistributed. Suggested tours are planning aids: always check difficulty, conditions, weather and official sources before you go.
