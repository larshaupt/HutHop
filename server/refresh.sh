#!/usr/bin/env sh
# Refresh source data on server2 and rebuild the route graph when needed, in a one-off container next to the
# running app (same image, same data folder). The app picks up the new graph by itself (data_version).
#
#   /opt/huthop/server/refresh.sh               huts + stops, trails if swisstopo has a new edition, rebuild if needed
#   /opt/huthop/server/refresh.sh --rebuild     force a rebuild
#
# A rebuild needs about 2 GB of RAM for ~2 minutes and ~650 MB of disk in data/tlm/. A backup is taken first.
set -eu
cd "$(dirname "$0")"
docker compose exec -T huthop python -m app.backup
docker compose run --rm --no-deps -T huthop python -m app.ingest.refresh "$@"
