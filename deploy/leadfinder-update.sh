#!/bin/sh
set -eu

APP_DIR=${LEADFINDER_DIR:-/opt/leadfinder}
BRANCH=${LEADFINDER_BRANCH:-main}

cd "$APP_DIR"
git fetch --quiet origin "$BRANCH"

remote=$(git rev-parse "origin/$BRANCH")
deployed=$(cat .leadfinder-deployed-sha 2>/dev/null || true)
if [ "$deployed" = "$remote" ]; then
    exit 0
fi

git merge --ff-only "$remote"
if [ "${LEADFINDER_WEB:-0}" = "1" ]; then
    set -- -f compose.yaml -f compose.web.yaml
    apps="leadfinder web"
else
    set -- -f compose.yaml
    apps=leadfinder
fi
docker compose "$@" build --pull
# `run --no-deps` below does not start dependencies: bring the database up and healthy first.
# --no-recreate: an update never restarts a running database; db config changes are applied by hand (DEPLOY.md).
docker compose "$@" up -d --no-recreate --wait --wait-timeout 120 db
if [ -f leadgen/web_migrate.py ]; then
    docker compose "$@" run --rm --no-deps leadfinder python -m leadgen.web_migrate
fi
# --no-deps: only the app containers are (re)created; the database keeps running untouched.
# shellcheck disable=SC2086
docker compose "$@" up -d --remove-orphans --no-deps --wait --wait-timeout 180 $apps
docker compose "$@" ps
printf '%s\n' "$remote" > .leadfinder-deployed-sha
