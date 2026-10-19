#!/bin/sh
# Nightly logical backup of the self-hosted Postgres (compose service db): pg_dump -Fc of schema leadgen
# (all application data, including web_schema_versions) into $BACKUP_DIR, checked with pg_restore --list
# before it is kept. Old leadgen-*.dump files are removed only after a new backup succeeded. Restore: DEPLOY.md, «Восстановление из резервной копии».
set -eu

APP_DIR=${LEADFINDER_DIR:-/opt/leadfinder}
BACKUP_DIR=${LEADFINDER_BACKUP_DIR:-$APP_DIR/backups}
KEEP_DAYS=${LEADFINDER_BACKUP_KEEP_DAYS:-14}
DB_NAME=leadgen

case "$KEEP_DAYS" in ''|*[!0-9]*) echo "LEADFINDER_BACKUP_KEEP_DAYS должен быть целым числом дней." >&2; exit 1;; esac

cd "$APP_DIR"
umask 077
mkdir -p "$BACKUP_DIR"

target="$BACKUP_DIR/$DB_NAME-$(date -u +%Y%m%dT%H%M%SZ).dump"
partial="$target.partial"
trap 'rm -f "$partial"' EXIT

# The db service lives in compose.yaml; exec reaches it whichever stack (bot only or bot + web) is running.
# Same shape as the Supabase dump in DEPLOY.md: roles and schema extensions come from deploy/postgres/init,
# so a backup restores the same way, as the app role, into a freshly initialised volume.
docker compose -f compose.yaml exec -T db pg_dump -U postgres -d "$DB_NAME" --schema=leadgen -Fc > "$partial"
if [ ! -s "$partial" ]; then
    echo "pg_dump вернул пустой файл." >&2
    exit 1
fi
# A truncated or corrupt archive fails here instead of silently replacing good backups.
docker compose -f compose.yaml exec -T db pg_restore --list < "$partial" > /dev/null
mv "$partial" "$target"
trap - EXIT

find "$BACKUP_DIR" -maxdepth 1 -type f -name "$DB_NAME-*.dump" -mtime +"$KEEP_DAYS" -exec rm -f {} +
echo "Резервная копия: $target ($(du -h "$target" | cut -f1))"
