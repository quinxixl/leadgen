#!/bin/sh
set -eu

APP_DIR=${LEADFINDER_DIR:-/opt/leadfinder}
BRANCH=${LEADFINDER_BRANCH:-main}

cd "$APP_DIR"
git fetch --quiet origin "$BRANCH"

current=$(git rev-parse HEAD)
remote=$(git rev-parse "origin/$BRANCH")
if [ "$current" = "$remote" ]; then
    exit 0
fi

git merge --ff-only "$remote"
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
