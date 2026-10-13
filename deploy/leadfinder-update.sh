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
else
    set -- -f compose.yaml
fi
docker compose "$@" build --pull
if [ -f leadgen/web_migrate.py ]; then
    docker compose "$@" run --rm --no-deps leadfinder python -m leadgen.web_migrate
fi
docker compose "$@" up -d --remove-orphans --wait --wait-timeout 120
docker compose "$@" ps
printf '%s\n' "$remote" > .leadfinder-deployed-sha
