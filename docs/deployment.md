# HutHop deployment plan

As of 2026-10-18. Living version: [HutHop Deployment Plan](https://claude.ai/code/artifact/364c19c3-4694-41fd-be7b-312d31dc81ed).

## Summary

HutHop runs on the home server `server2` (larsbox) with Docker Compose and is reachable only over Tailscale (option A). It is deployed the same way as the website: GitHub Actions builds the image, and the server pulls it every 5 minutes. The design brief already planned a home server ("Docker Compose only once it is deployed to a home server"), and this keeps the SAC data private without any login code in the app.

- **Push to deploy.** A push to `main` runs the tests and publishes `ghcr.io/larshaupt/huthop:latest`. Cron on the server picks it up within 5 minutes.
- **Serve at home, build data on the Mac.** The server runs one FastAPI container and a SQLite file. The heavy route graph build (`build_edges`, about 2 GB RAM) stays on the Mac.
- **Private by network.** The container listens on localhost only, and the host's Tailscale publishes it to the tailnet. Nothing is exposed to the internet.

## App profile

Serving HutHop takes little: a few hundred MB of RAM and a fraction of a CPU. Only the yearly route graph build is heavy.

| Part | What it is | Why it matters for deployment |
| --- | --- | --- |
| Web app | One FastAPI/uvicorn process (`app/main.py`); the static frontend in `web/` needs no build step | One container is enough |
| Database | SQLite `data/huthop.db`, about 21 MB; path set with `HUTHOP_DB` | One persistent volume; no database server |
| `sync_sac` | Fetches SAC huts, about 10 s and 5 requests | Can run on the server on a schedule |
| `load_stops` | Public transport stops, about 5 MB download | Rare; run on the Mac |
| `build_edges` | Route graph on swissTLM3D: about 90 s, about 2 GB RAM, 651 MB of input in `data/tlm/` | Yearly; run on the Mac |
| Writes while running | `PATCH /api/edges/{id}` now; availability cache and price admin page in Phase 4 | The server's database is not read-only |
| Browser dependencies | MapLibre from unpkg, swisstopo tiles | The browser needs internet access; the server doesn't serve them |
| Server calls out to | SAC API now; hut-reservation.org in Phase 4 | Outgoing internet only; nothing needs to accept calls from outside |

## Constraints

Any deployment has to keep the app private, because the SAC terms allow private use only.

1. **No public access.** SAC hut data may not be redistributed (brief §6). The network provides access control (tailnet only). Inside the tailnet, everyone can view and plan; **changing data needs the admin password** (`app/admin.py`, HTTP Basic auth over Tailscale's HTTPS). `server/compose.yaml` sets `HUTHOP_REQUIRE_ADMIN=1`, so changes are refused until a password is set.
2. **Build and serving are separate.** `build_edges` needs 2 GB of RAM and 651 MB of input for a job that runs once a year. Building on the Mac and copying the finished `huthop.db` keeps the server small.
3. **Server-side edits must survive a rebuild.** Edge exclusions, and later prices, are written on the server. Uploading a newly built database must not overwrite them (see Data workflow).
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
│  ┌──────────────────┐    │  ┌─────────▼──────────────────────┐    │ │
│  │ Mac (build)      │◀──────▶ /opt/huthop/data               │    │ │
│  │ build_edges,     │ scp│  │ huthop.db (SQLite)             │    │ │
│  │ load_stops       │    │  └────────────────────────────────┘    │ │
│  └──────────────────┘    └────────────────────────────────────────┘ │
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

The Mac builds the database and the server owns it afterwards. Before each rebuild, the server's copy is pulled back so manual edits aren't lost.

**First deploy**

1. Run `sync_sac`, `load_stops` and `build_edges` on the Mac, as today.
2. Copy `data/huthop.db` to `/opt/huthop/data/` on the server (`scp` over Tailscale) while the container is stopped.
3. Start the stack. From then on the server's copy is the live one.

**Yearly route graph rebuild (new swissTLM3D edition)**

1. Run `python -m app.backup` in the container and copy that file from `data/backups/` back to the Mac.
2. Run `build_edges` against that copy. Manual `excluded` flags on edges already survive rebuilds.
3. Copy the result back and restart.

**Longer term:** move manual edits (edge exclusions, notes, prices) into override tables that a rebuild never touches, like `hut_override`. Then the server can receive a freshly built database, and the edits are re-applied on top. Only needed if the pull-back step gets annoying.

## Code changes before deploy

None of these touches the app's behaviour.

- [x] `Dockerfile`, `server/compose.yaml`, `server/update.sh` and the GitHub Actions workflow (see Target setup).
- [x] SQLite WAL mode in `connect()` (`PRAGMA journal_mode=WAL`), so reads keep working while the availability cache writes. The database is then more than one file (`huthop.db-wal`, `-shm`): copy it with `python -m app.backup` or with the container stopped, never with a plain `scp` while it runs.
- [x] `GET /api/health` for the Docker health check and monitoring (answers with hut and edge counts).
- [x] Cache the route graph per trail limit (`cached_graph`). Triggers bump `data_version` on every change to huts, overrides, stops and edges, from any process, and the next search reloads.

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
| SAC sync | Weekly or monthly | Host cron: `docker compose -f /opt/huthop/server/compose.yaml exec huthop python -m app.ingest.sync_sac` |
| Availability | On demand | Fetched per hut and cached for a few hours by the app (Phase 4); no job |
| Route graph rebuild | Yearly, by hand | On the Mac, see Data workflow |
| Backup | Nightly | Host cron: `cd /opt/huthop/server && docker compose exec -T huthop python -m app.backup >> /opt/huthop/backup.log 2>&1`. Writes `data/backups/huthop-YYYY-MM-DD.db`, keeps 14. Sometimes copy one off the server |
| Monitoring | Ongoing | Docker health check; optionally a healthchecks.io ping after `update.sh`, like the website |

## Rollout steps

- [x] Machine: `server2` (larsbox) with Docker and Tailscale already installed
- [ ] Commit and push the deploy files; check that the Actions run publishes the image
- [ ] Clone the repo to `/opt/huthop` and copy `huthop.db` into `/opt/huthop/data/`
- [ ] `docker compose pull && docker compose up -d` in `/opt/huthop/server`
- [ ] `tailscale serve --bg --https=8443 http://127.0.0.1:8100`, then open the URL from a phone over mobile data
- [ ] Check that the app can't be reached from the LAN IP or the public IP
- [ ] Add the cron jobs for `update.sh`, SAC sync and backups
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
- [ ] Deploy now, with Phases 0–3, or wait for Phase 4 (availability, prices)?
- [ ] Should backups also go off the machine (external drive, cloud)?
