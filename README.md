# HutHop

A private web app to browse Swiss mountain huts (SAC plus huts added by hand) and to suggest multi-day summer hut tours from your constraints, with live free-bed checks.

**Status (Oct 2026):** deployed at huthop.larshauptmann.com (Oracle VM, Cloudflare Access login). Hut map with filters, free-beds calendar and hand
corrections; walking connections on swisstopo's trail network; multi-day tour suggestions ranked by a fixed "best"
score (new trail, shape, free beds, opening data, …), sortable in the browser; height profiles; GPX export; weather forecast, trail closures and
military shooting notices per day; favourite huts; About dialog. Prices are postponed. See [`hut-tour-planner.md`](hut-tour-planner.md) for the project brief (requirements,
design, APIs, plan) and [`spikes/`](spikes/) for the Phase 0 findings.

![Map of all huts with a line for each pair connected by trail within 10 hours, coloured by the hardest trail category](docs/hut-graph.svg)

The route graph built by `build_edges`: one line per hut pair connected within 10 h, the longest connection it
stores. Hollow huts have no trail connection, usually because they are too far from a signposted trail.
Bus and train stops are left out. Regenerate the figure with `uv run python docs/graph_figure.py`.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```sh
uv sync                                   # create .venv and install dependencies
uv run python -m app.ingest.refresh       # huts, stops, timetable, trails (if new) and route graph (if needed)
uv run uvicorn app.main:app --reload      # http://localhost:8000
uv run pytest                             # tests (use a throwaway database, see tests/conftest.py)
uv run playwright install chromium        # once: the browser for the mobile tests (tests/test_mobile.py)
```

`refresh` runs the individual jobs (`sync_sac`, `load_stops`, `load_timetable`, `build_edges`), downloads a new swissTLM3D edition
only when swisstopo publishes one, and rebuilds the route graph only when hut/stop positions or trails changed
(`--rebuild` forces it). Each run is logged to `data/refresh_log.json`. Run it weekly during the season.
`sync_sac --from-file data/raw/sac/huts.json` re-imports the last saved API response without fetching.

Deployment (Oracle VM, Cloudflare tunnel and Access), backups and the data workflow: see [`docs/deployment.md`](docs/deployment.md).
Changing data needs the admin password once one is set (`python -m app.admin set-password`); on the server,
changes are refused until then.

Settings: `huthop.toml`, read on every request and baked into the Docker image: the weights of the "best" tour
order (shown in the app's About dialog) and the impressum. Manual corrections: "Edit hut data" in a hut's detail
view, "+ Add hut" in the hut list, and excluding a connection in a hut's connection list; all survive re-syncs and
rebuilds. New huts get walking connections at the next `refresh`.

`refresh` downloads the swissTLM3D hiking network (swisstopo open data, about 200 MB) into `data/tlm/` when needed,
and the official timetables (GTFS, opentransportdata.swiss, about 90 MB each: this year's and, from autumn, next
year's) into `data/gtfs/` when a new edition is out. They give the lines and summer departures of each stop (a hut
only gets a connection to the closest stop of each line) and on which days and from when to when each stop is
served: tours start and end at the closest stop that runs on that day.
On the server, `server/refresh.sh` does the same in a one-off container (see the deployment doc). Edges without a stored
footprint (grid cells and shape samples for the tour search) get one with `python -m app.ingest.footprints`, which
`refresh` also runs.

Other commands: `python -m app.backup` (dated copy in `data/backups/`, keeps 14), `python -m app.admin set-password`.

## Contributing

No secrets or accounts are needed to run HutHop locally: `refresh` fetches all data from public sources, and
locally no admin password is required. Only `python -m app.ingest.scrape_huts` (prices and season dates from
the hut websites) needs Gemini on Vertex AI via your own `gcloud` login and project (`GEMINI_PROJECT`).

Work on a branch and open a pull request against `main`. CI runs `uv run ruff check .` and `uv run pytest -q` on
every pull request (including the mobile browser tests, with screenshots of failures as a build artifact);
run both before pushing. A merge into `main` builds the Docker image and deploys it to the live
server, so `main` only changes through reviewed pull requests.

## Layout

```
app/
  main.py    FastAPI app: routers, /api/health, static frontend (revalidated on every load)
  admin.py   admin password (HTTP Basic) for data-changing routes; `python -m app.admin set-password`
  backup.py  database backup; config.py: huthop.toml
  api/       routes: huts (list, detail, /availability calendar, /weather), edges (connections, exclude), manual
             (overrides, added huts), tours (/suggest, /{id}, /{id}/gpx, /{id}/warnings, /{id}/options for the editor, /huts/{id}/tour, /stops),
             info (About dialog), feedback (sent from the app; reading it needs the admin password)
  db/        schema.sql (tables, views, change-counter triggers) and connect() (migrations, WAL)
  ingest/    offline jobs: refresh, sync_sac, load_stops, load_timetable, build_edges, footprints
  planner/   graph (model, loading, cache), tours (beam search, scoring, rebuilding, diversity),
             planning (search with free-bed rounds, rebuild by id: what the API calls), ranking (sub-scores,
             "best"), geometry (retracing, shape), availability (cache, classification, calendar), opening,
             transit (stops served on a date), editing (tour editor: possible edits, warnings), gpx,
             hiking_time, polyline
  clients/   external APIs (SAC, hut-reservation.org, weather via Open-Meteo, geo.admin closures and
             shooting notices)
web/         static frontend (MapLibre + swisstopo tiles, no build step), ES modules in web/js/:
             main (wiring), state (URL hash), map, huts (list, detail), beds (free-beds calendar), weather,
             connections, tours (form, results, sorting, opening a tour), tour-detail (timeline, profiles),
             tour-editor (editing a tour), your-tours and favourites (kept in the browser), settings (general
             settings and units, kept in the browser), editor (hut data),
             info (About dialog), feedback (dialog), util
server/      compose.yaml, update.sh (deploy) and refresh.sh (data refresh and graph build) for the server
docs/        deployment plan, route graph figure
spikes/      Phase 0 experiments and findings (historical)
tests/
```

## Usage and data

Private, non-commercial use only. Hut data comes from the SAC (© sac-cas.ch) and must not be redistributed. Maps and trail data © swisstopo. Suggested tours are planning aids: always check difficulty, conditions, weather and official sources before you go.
