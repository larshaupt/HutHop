# HutHop deployment plan

As of 2026-10-18. Living version: [HutHop Deployment Plan](https://claude.ai/code/artifact/364c19c3-4694-41fd-be7b-312d31dc81ed).

## Summary

HutHop will run first on a home server, using Docker Compose, and be reachable only over Tailscale (option A). The design brief already planned this ("Docker Compose only once it is deployed to a home server"). It also keeps the SAC data private without any login code in the app.

- **Serve at home, build on the Mac.** The server runs one FastAPI container and a SQLite file. The heavy route graph build (`build_edges`, about 2 GB RAM) stays on the Mac.
- **Private by network.** Only devices in your Tailscale network can reach the app, and nothing is exposed to the internet.
- **Little to run.** One container, one volume, a nightly backup and a periodic SAC sync.

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

1. **No public access.** SAC hut data may not be redistributed (brief §6). The app has no login, and `PATCH /api/edges` lets anyone change data. The network has to provide access control.
2. **Build and serving are separate.** `build_edges` needs 2 GB of RAM and 651 MB of input for a job that runs once a year. Building on the Mac and copying the finished `huthop.db` keeps the server small.
3. **Server-side edits must survive a rebuild.** Edge exclusions, and later prices, are written on the server. Uploading a newly built database must not overwrite them (see Data workflow).
4. **Be a polite API consumer.** Use low request rates, caching and an identifiable User-Agent toward SAC and hut-reservation.org. That applies equally to the server.

## Target setup (option A)

One always-on machine at home runs the HutHop container plus a Tailscale client, with the SQLite file on a host volume.

```
┌─ Your tailnet (private network) ──────────────────────────────┐
│                          ┌─ Home server (Docker Compose) ───┐ │
│  ┌──────────────────┐    │  ┌────────────────────────────┐  │ │
│  │ Your devices     │───────▶ tailscale sidecar          │  │ │
│  │ Mac, phone,      │    │  │ HTTPS on huthop.*.ts.net   │  │ │
│  │ friends          │    │  │ no ports open to LAN/router│  │ │
│  └──────────────────┘    │  └─────────────┬──────────────┘  │ │     Internet (outgoing only)
│                          │  ┌─────────────▼──────────────┐  │ │    ┌─────────────────────┐
│                          │  │ huthop container           │──────────▶ SAC API             │
│                          │  │ FastAPI + web/ on :8000    │  │ │    └─────────────────────┘
│                          │  │ cron: sync_sac weekly      │──────────▶ hut-reservation.org │
│                          │  └─────────────▲──────────────┘  │ │    │ (Phase 4)           │
│  ┌──────────────────┐    │  ┌─────────────▼──────────────┐  │ │    └─────────────────────┘
│  │ Mac (build)      │◀──────▶ ./data volume              │  │ │
│  │ build_edges,     │ scp│  │ huthop.db (SQLite, WAL)    │  │ │
│  │ load_stops       │    │  │ nightly .backup copies     │  │ │
│  └──────────────────┘    │  └────────────────────────────┘  │ │
│                          └──────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
```

The browser loads swisstopo tiles and MapLibre directly. The server only makes outgoing calls.

**Hardware.** Any machine that stays on works: a Raspberry Pi 5 (4 GB or more), a NAS that runs Docker, or an old laptop. Serving needs under 1 GB of RAM. With 4 GB or more the machine could also run `build_edges`, but the plan doesn't depend on it.

**Containers (Docker Compose).**

| Service | Image | Purpose |
| --- | --- | --- |
| `huthop` | Built from the repo (uv base image, Python 3.12) | Runs `uvicorn app.main:app` on port 8000, with `./data` mounted at `/app/data` |
| `tailscale` | `tailscale/tailscale` | Joins your tailnet as host `huthop`; `tailscale serve` adds HTTPS at `https://huthop.<tailnet>.ts.net` |

Alternative: install Tailscale on the host itself and bind the app to the Tailscale interface only. That's simpler on a Pi; the sidecar is tidier on a NAS.

**Access.** Port 8000 is never published to the LAN or the router. Your devices and invited friends join the tailnet, and the iOS and Android apps work for phones on a hike. Tailscale ACLs can limit friends to the `huthop` host only.

## Data workflow

The Mac builds the database and the server owns it afterwards. Before each rebuild, the server's copy is pulled back so manual edits aren't lost.

**First deploy**

1. Run `sync_sac`, `load_stops` and `build_edges` on the Mac, as today.
2. Copy `data/huthop.db` to the server's `./data/` (for example `scp` over Tailscale) while the container is stopped.
3. Start the stack. From then on the server's copy is the live one.

**Yearly route graph rebuild (new swissTLM3D edition)**

1. Stop the container, or take a SQLite `.backup` copy, and copy the server's `huthop.db` back to the Mac.
2. Run `build_edges` against that copy. Manual `excluded` flags on edges already survive rebuilds.
3. Copy the result back and restart.

**Longer term:** move manual edits (edge exclusions, notes, prices) into override tables that a rebuild never touches, like `hut_override`. Then the server can receive a freshly built database, and the edits are re-applied on top. Only needed if the pull-back step gets annoying.

## Code changes before deploy

Five small changes; none touches the app's behaviour.

- [ ] `Dockerfile`: uv base image, `uv sync --frozen --no-dev`, copy `app/` and `web/`, run uvicorn on `0.0.0.0:8000`. The ingest-only dependencies (pyogrio, scipy) can stay for now.
- [ ] `compose.yaml`: `huthop` plus the `tailscale` sidecar, `./data` volume, `restart: unless-stopped`. The Tailscale auth key goes in an untracked `.env`, never in the repo.
- [ ] SQLite WAL mode in `connect()` (`PRAGMA journal_mode=WAL`), so reads keep working while the availability cache writes.
- [ ] `GET /api/health` for the Docker health check and monitoring.
- [ ] Cache `load_graph()` per trail limit, cleared when edges are changed. It rebuilds on every `/api/tours/suggest` call today, which will be noticeable on a Pi.

## Operations

The recurring work comes down to a few host cron jobs and a `git pull` when deploying.

| Task | How often | How |
| --- | --- | --- |
| SAC sync | Weekly or monthly | Host cron: `docker compose exec huthop python -m app.ingest.sync_sac` |
| Availability | On demand | Fetched per hut and cached for a few hours by the app (Phase 4); no job |
| Route graph rebuild | Yearly, by hand | On the Mac, see Data workflow |
| Backup | Nightly | `sqlite3 data/huthop.db ".backup ..."` to a dated file, keep about 14 copies, sometimes copy one off the server |
| App update | When you ship | `git pull && docker compose up -d --build` on the server |
| Monitoring | Ongoing | Docker health check on `/api/health`; optionally a free uptime ping over Tailscale |

## Rollout steps

- [ ] Pick the machine and install Docker plus the Compose plugin
- [ ] Make the code changes above and commit them
- [ ] Create a Tailscale account, add the Mac and phone, and generate an auth key for the server
- [ ] Clone the repo on the server and put the auth key in `.env`
- [ ] Copy `huthop.db` from the Mac into the server's `./data/`
- [ ] `docker compose up -d --build` and open `https://huthop.<tailnet>.ts.net` from a phone over mobile data
- [ ] Check that the app can't be reached from the LAN IP or the public IP
- [ ] Set up the cron jobs for SAC sync and backups
- [ ] Invite friends to the tailnet and add an ACL limiting them to `huthop`

## Alternatives considered

All three are kept as fallbacks; none beats a home server for a private app.

| Option | Fit | Why not first |
| --- | --- | --- |
| Small VPS (Hetzner CX22 about €4/month, or a Swiss host like Infomaniak) + Caddy + Tailscale or Cloudflare Access | Good: same Compose stack, no home hardware | Monthly cost; server IP may get rate-limited by upstream APIs (unverified) |
| Hosting platform (Fly.io with a volume, Railway) | OK | SQLite needs a volume, access control is up to you, uploading the database is clumsy |
| Serverless (Vercel, Lambda) | Poor | Runtime SQLite writes and heavy native dependencies (scipy, shapely) |

Moving from A to a VPS later is a matter of copying the Compose folder and the database.

## Open questions

- [ ] Which machine: Pi 5, NAS or an old laptop?
- [ ] Is installing Tailscale fine for friends, or do they need a plain link with a password?
- [ ] Deploy now, with Phases 0–3, or wait for Phase 4 (availability, prices)?
- [ ] Should backups also go off the machine (external drive, cloud)?
