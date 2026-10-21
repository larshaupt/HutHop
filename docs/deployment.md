# HutHop deployment

Live on server2 since 2026-10-18; this file is kept up to date (last: 2026-10-19). The original plan is in the
artifact [HutHop Deployment Plan](https://claude.ai/code/artifact/364c19c3-4694-41fd-be7b-312d31dc81ed), which is
not updated any more.

## Summary

HutHop runs on the home server `server2` (larsbox) with Docker Compose and is reachable only over Tailscale (option A). It is deployed the same way as the website: GitHub Actions builds the image, and the server pulls it every 5 minutes. The design brief already planned a home server ("Docker Compose only once it is deployed to a home server"), and the network keeps the SAC data private. The app only has an admin password for changing data.

- **Push to deploy.** A push to `main` runs the tests and publishes `ghcr.io/larshaupt/huthop:latest`. Cron on the server picks it up within 5 minutes.
- **Everything on the server.** One FastAPI container and a SQLite file. Data refreshes and the route graph build (about 2 GB RAM for ~2 minutes) run in a one-off container from the same image (`server/refresh.sh`).
- **Private by network.** The container listens on localhost only, and the host's Tailscale publishes it to the tailnet. Nothing is exposed to the internet.

## App profile

Serving HutHop takes little: a few hundred MB of RAM and a fraction of a CPU. Only the yearly route graph build is heavy.

| Part | What it is | Why it matters for deployment |
| --- | --- | --- |
| Web app | One FastAPI/uvicorn process (`app/main.py`); the static frontend in `web/` needs no build step | One container is enough |
| Database | SQLite `data/huthop.db`, about 36 MB, WAL mode; path set with `HUTHOP_DB` | One persistent volume; no database server |
| `sync_sac` | Fetches SAC huts, about 10 s and 5 requests | Can run on the server on a schedule |
| `load_stops` | Public transport stops, about 5 MB download | Rare; `server/refresh.sh` |
| `load_timetable` | Lines, summer departures and service days per stop (GTFS, this year's and next year's timetable), about 90 MB download each when a new weekly edition is out, ~95 s and ~600 MB RAM to read both (locally; slower on server2) | Weekly; `server/refresh.sh` (rebuilds the graph only if lines changed) |
| `build_edges` | Route graph on swissTLM3D: about 2 min, about 2 GB RAM, 651 MB of input in `data/tlm/` | Yearly (new trail edition) or after new huts; `server/refresh.sh` |
| Writes while running | Availability cache; hut corrections, added huts and edge exclusions (admin password) | The server's database is not read-only, and it is the master copy |
| Settings | `huthop.toml` ("best" weights, impressum), baked into the image | Changing it means a commit and deploy |
| Memory | Route graph cached per trail limit, plus a bounded cache of route-line overlaps (~40 MB at most) | A few hundred MB in total |
| Browser dependencies | MapLibre from unpkg, swisstopo tiles | The browser needs internet access; the server doesn't serve them |
| Server calls out to | SAC API (weekly sync), swisstopo, BAV and opentransportdata.swiss (weekly refresh), hut-reservation.org (free beds, on demand) | Outgoing internet only; nothing needs to accept calls from outside |

## Constraints

Any deployment has to keep the app private, because the SAC terms allow private use only.

1. **No public access.** SAC hut data may not be redistributed (brief §6). The network provides access control (tailnet only). Inside the tailnet, everyone can view and plan; **changing data needs the admin password** (`app/admin.py`, HTTP Basic auth over Tailscale's HTTPS). `server/compose.yaml` sets `HUTHOP_REQUIRE_ADMIN=1`, so changes are refused until a password is set.
2. **Build and serving share the database, not the process.** `build_edges` needs 2 GB of RAM and 651 MB of input for a job that runs about once a year. It runs in its own short-lived container, so the app's memory stays small, and it writes to the live database (WAL mode lets the app keep serving).
3. **Server-side edits must survive a rebuild.** Hut corrections, added huts and edge exclusions (later prices) are written on the server. Rebuilds keep them (overrides are separate tables; exclusions are carried over by place pair).
4. **Be a polite API consumer.** Use low request rates, caching and an identifiable User-Agent toward SAC and hut-reservation.org. That applies equally to the server.

## Target setup (option A)

`server2` (larsbox: x86_64, Debian 13, 6.7 GB RAM) runs the HutHop container next to the website, Immich and Paperless. Tailscale already runs on the host.

```
  GitHub: push to main ──▶ Actions: pytest, build ──▶ ghcr.io/larshaupt/huthop:latest
                                                                │ pulled every 5 min
┌─ Your tailnet (private network) ──────────────────────────────┼─────┐
│                          ┌─ server2 (larsbox) ────────────────▼───┐ │
│  ┌──────────────────┐    │  host tailscale serve (not funnel)     │ │
│  │ Your devices     │───────▶ https://larsbox.<tailnet>.ts.net:8443 │
│  │ Mac, phone,      │    │            │                           │ │
│  │ friends          │    │  ┌─────────▼──────────────────────┐    │ │     Internet (outgoing only)
│  └──────────────────┘    │  │ huthop container               │─────────▶ SAC API
│                          │  │ FastAPI + web/, 127.0.0.1:8100 │─────────▶ hut-reservation.org
│                          │  └─────────▲──────────────────────┘    │ │
│                          │  ┌─────────▼──────────────────────┐    │ │
│                          │  │ /opt/huthop/data               │    │ │
│                          │  │ huthop.db backups/ tlm/ gtfs/  │    │ │
│                          │  └─────────▲──────────────────────┘    │ │
│                          │  ┌─────────┴──────────────────────┐    │ │
│                          │  │ refresh.sh: one-off container  │─────────▶ SAC, swisstopo, BAV, SBB
│                          │  │ (data refresh, graph build)    │    │ │
│                          │  └────────────────────────────────┘    │ │
│                          └────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

The browser loads swisstopo tiles and MapLibre directly. The server only makes outgoing calls.

**Files in the repo.**

| File | Purpose |
| --- | --- |
| `Dockerfile` | uv base image, Python 3.12, runtime dependencies only; runs uvicorn on port 8000 as uid 1000. Contains code only, no data |
| `.github/workflows/deploy.yml` | On push to `main`: run tests, build for linux/amd64, push `latest` and `sha-…` tags to GHCR |
| `server/compose.yaml` | Pulls the image, publishes `127.0.0.1:8100`, mounts `../data`, read-only root filesystem, no capabilities, health check |
| `server/update.sh` | `git pull`, `docker compose pull`, `up -d`, prune old images (same as the website) |

**On the server.** The repo is cloned to `/opt/huthop`, next to `/opt/website`. The database lives in `/opt/huthop/data/` (gitignored, owned by `lars`, uid 1000). Port 8000 is taken by another service, hence 8100.

**Access.** The container is bound to localhost only. `tailscale serve --bg --https=8443 http://127.0.0.1:8100` on the host publishes it at `https://larsbox.<tailnet>.ts.net:8443`, inside the tailnet only (never `tailscale funnel`, and not through the website's Cloudflare tunnel). Your devices and invited friends join the tailnet, and the iOS and Android apps work for phones on a hike. Tailscale ACLs can limit friends to that port on `larsbox` only.

## Data workflow

The server's database is the only live copy. Everything that changes it runs on the server:

```sh
/opt/huthop/server/refresh.sh            # backup, then huts, stops, timetable, trails if new, rebuild if needed
/opt/huthop/server/refresh.sh --rebuild  # force a rebuild (e.g. after adding huts by hand)
```

`refresh.sh` takes a backup, then runs `python -m app.ingest.refresh` in a one-off container (the `refresh`
service in `compose.yaml`: no web server, memory capped at 3 GB, progress printed live). The first run (19 Oct
2026) downloaded the swissTLM3D trails (198 MB, 372 MB unpacked in `data/tlm/`) and rebuilt all 31,390 edges in
12 minutes; at least 2.1 GB of RAM stayed free and the app kept serving. The running app notices the new data
through `data_version`; its first search afterwards takes ~10 s while it loads the new graph, then ~2 s.

For local development, copy a backup from `data/backups/` to the Mac.

**Hut websites** (prices, season dates, booking; once or twice per season) are the exception: reading them needs
Gemini through the gcloud login on the Mac. Fetch and extract on the Mac, then load the results on the server
(only the `.json` files are needed, not `pdf/`):

```sh
uv run python -m app.ingest.scrape_huts fetch && uv run python -m app.ingest.scrape_huts extract   # Mac
(cd data/raw/web && COPYFILE_DISABLE=1 tar -cf - --no-xattrs *.json) | \
  ssh server2 'mkdir -p /opt/huthop/data/raw/web && tar -xf - -C /opt/huthop/data/raw/web'          # Mac (no rsync on server2)
cd /opt/huthop/server && docker compose exec -T huthop python -m app.backup && \
  docker compose run --rm -T refresh python -m app.ingest.scrape_huts load                       # server
```

**History:** until 2026-10-19 the graph was built on the Mac and the database copied to the server (first deploy
on 2026-10-18; height profiles imported once on 2026-10-19).

## Server-related code

- `Dockerfile`, `server/compose.yaml`, `server/update.sh` and the GitHub Actions workflow (see Target setup).
- SQLite WAL mode in `connect()` (`PRAGMA journal_mode=WAL`), so reads keep working while the availability cache writes. The database is then more than one file (`huthop.db-wal`, `-shm`): copy it with `python -m app.backup` or with the container stopped, never with a plain `scp` while it runs.
- `GET /api/health` for the Docker health check and monitoring (answers with hut and edge counts).
- The frontend is served with `Cache-Control: no-cache`, so browsers pick up a deploy on a normal reload.
- Cache the route graph per trail limit (`cached_graph`). Triggers bump `data_version` on every change to huts, overrides, stops and edges, from any process, and the next search reloads.

## Admin password

Set it once on the server (the hash is stored in `/opt/huthop/data/admin_password`, never in git):

```sh
cd /opt/huthop/server && docker compose exec huthop python -m app.admin set-password
```

The browser asks for it the first time you edit a hut or exclude a connection; any username works.
Run the same command again to change it. On the Mac, editing stays open unless a password is set there.

## Operations

Deploying is a push to `main`; the rest is a few host cron jobs.

| Task | How often | How |
| --- | --- | --- |
| App update | Every 5 min | Host cron: `/opt/huthop/server/update.sh >> /opt/huthop/update.log 2>&1`. The script waits 150 s so it doesn't overlap the website's update (whose `docker image prune` broke our pulls), retries the pull once, and prunes only HutHop images |
| Data refresh | Mondays 4:00 | Host cron (since 2026-10-19; before: SAC sync only): `/opt/huthop/server/refresh.sh >> /opt/huthop/refresh.log 2>&1`. Backup, SAC huts, stops, timetable, trails if swisstopo has a new edition, route graph rebuild only if something moved or the lines per stop changed |
| Availability | On demand | Fetched per hut and cached for 10 minutes by the app; no job |
| Route graph rebuild | When swisstopo publishes new trails (about yearly), or after adding huts | Automatic in the Monday refresh when trails or positions changed; after adding huts by hand: `server/refresh.sh --rebuild` |
| Backup | Nightly 3:30 | Host cron: `cd /opt/huthop/server && docker compose exec -T huthop python -m app.backup >> /opt/huthop/backup.log 2>&1`. Writes `data/backups/huthop-YYYY-MM-DD.db`, keeps 14. Sometimes copy one off the server |
| Monitoring | Ongoing | Docker health check on `/api/health`; optionally a healthchecks.io ping after `update.sh`, like the website |

## Rollout steps

- [x] Machine: `server2` (larsbox) with Docker and Tailscale already installed
- [x] Commit and push the deploy files; check that the Actions run publishes the image
- [x] Clone the repo to `/opt/huthop` and copy `huthop.db` into `/opt/huthop/data/`
- [x] `docker compose pull && docker compose up -d` in `/opt/huthop/server`
- [x] `tailscale serve --bg --https=8443 http://127.0.0.1:8100`, then open the URL from a phone
- [x] Check that the app can't be reached from the LAN IP or the public IP
- [x] Add the cron jobs for `update.sh`, SAC sync and backups
- [x] Set the admin password
- [ ] Invite friends to the tailnet and add an ACL limiting them to `larsbox:8443`

## Alternatives considered

All three are kept as fallbacks; none beats a home server for a private app.

| Option | Fit | Why not first |
| --- | --- | --- |
| Small VPS (Hetzner CX22 about €4/month, or a Swiss host like Infomaniak) + Caddy + Tailscale or Cloudflare Access | Good: same Compose stack, no home hardware | Monthly cost; server IP may get rate-limited by upstream APIs (unverified) |
| Hosting platform (Fly.io with a volume, Railway) | OK | SQLite needs a volume, access control is up to you, uploading the database is clumsy |
| Serverless (Vercel, Lambda) | Poor | Runtime SQLite writes and heavy native dependencies (scipy, shapely) |

Moving from A to a VPS later is a matter of copying the Compose folder and the database.

## Open questions

- [ ] Is installing Tailscale fine for friends, or do they need a plain link with a password?
- [ ] Should backups also go off the machine (external drive, cloud)?
