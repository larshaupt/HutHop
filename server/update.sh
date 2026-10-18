#!/usr/bin/env sh
# Pulls repo changes and the new image, restarts only what changed.
# Run from cron every 5 minutes.
set -eu
cd "$(dirname "$0")"
git -C .. pull --ff-only --quiet
docker compose pull --quiet
docker compose up -d --remove-orphans
docker image prune -f >/dev/null
