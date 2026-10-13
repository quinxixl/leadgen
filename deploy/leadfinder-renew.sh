#!/bin/sh
set -eu

APP_DIR=${LEADFINDER_DIR:-/opt/leadfinder}

cd "$APP_DIR"
exec docker compose -f compose.yaml -f compose.web.yaml exec -T web python -m leadgen.payments renew
