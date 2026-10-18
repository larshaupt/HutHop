#!/usr/bin/env sh
# Pulls repo changes and the new image, restarts only what changed.
# Run from cron every 5 minutes.
#
# The website's update.sh runs from the same cron schedule and prunes images. A prune during our pull deletes
# the half-downloaded image ("failed commit on ref", "lease does not exist"), so we start 150 s later, retry
# the pull once, and prune only HutHop's own images.
set -eu
cd "$(dirname "$0")"
sleep 150
git -C .. pull --ff-only --quiet
docker compose pull --quiet || { sleep 30; docker compose pull --quiet; }
docker compose up -d --remove-orphans
docker image prune -f --filter "label=org.opencontainers.image.source=https://github.com/larshaupt/HutHop" >/dev/null
