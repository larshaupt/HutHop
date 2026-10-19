#!/usr/bin/env sh
# Refresh source data on server2 and rebuild the route graph when needed, in a one-off container next to the
# running app (same image, same data folder). The app picks up the new graph by itself (data_version).
#
#   /opt/huthop/server/refresh.sh               huts + stops, trails if swisstopo has a new edition, rebuild if needed
#   /opt/huthop/server/refresh.sh --rebuild     force a rebuild
#
# A full rebuild takes ~12 minutes on server2 (loading the trails ~5 min) and ~2 GB of RAM (capped at 3 GB, see the
# refresh service in compose.yaml), plus ~400 MB of disk in data/tlm/. A backup is taken first.
set -eu
cd "$(dirname "$0")"
docker compose exec -T huthop python -m app.backup
docker compose run --rm -T refresh python -m app.ingest.refresh "$@"
