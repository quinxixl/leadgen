#!/bin/sh
set -eu

APP_DIR=${LEADFINDER_DIR:-/opt/leadfinder}
BRANCH=${LEADFINDER_BRANCH:-main}
WEB=${LEADFINDER_WEB:-0}

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите установку через sudo." >&2
    exit 1
fi

case "$BRANCH" in ''|*[!A-Za-z0-9._/-]*) echo "Некорректное имя ветки." >&2; exit 1;; esac
case "$WEB" in 0|1) :;; *) echo "LEADFINDER_WEB должен быть 0 или 1." >&2; exit 1;; esac

# The units run as this account, not root. It may use docker but owns nothing else on the server.
DEPLOY_USER=leadfinder-deploy
DEPLOY_HOME=/var/lib/leadfinder-deploy

if [ "$APP_DIR" != /opt/leadfinder ]; then
    echo "Юниты systemd рассчитаны на /opt/leadfinder; поправьте пути в deploy/*.service." >&2
    exit 1
fi
if ! getent group docker >/dev/null; then
    echo "Нет группы docker: сначала установите Docker." >&2
    exit 1
fi
case "$(git -c safe.directory="$APP_DIR" -C "$APP_DIR" remote get-url origin 2>/dev/null || true)" in
    https://*) :;;
    *) echo "Внимание: origin не HTTPS. Ключ доступа к GitHub должен лежать в $DEPLOY_HOME/.ssh." >&2;;
esac

if ! id -u "$DEPLOY_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$DEPLOY_HOME" --shell /usr/sbin/nologin "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"
install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DEPLOY_HOME"
# The checkout and .env belong to the deploy account. leadgen/data keeps the container user (uid 10001).
find "$APP_DIR" -path "$APP_DIR/leadgen/data" -prune -o ! -user "$DEPLOY_USER" \
    -exec chown -h "$DEPLOY_USER:$DEPLOY_USER" {} +
# docker compose build (as the deploy user) must be able to list leadgen/data, or BuildKit stops with
# "open leadgen/data: permission denied". Only the directory itself changes; files inside are left as is.
DATA_DIR="$APP_DIR/leadgen/data"
mkdir -p "$DATA_DIR"
chown 10001:"$DEPLOY_USER" "$DATA_DIR"
chmod 0750 "$DATA_DIR"

chmod 0755 "$APP_DIR/deploy/leadfinder-update.sh" "$APP_DIR/deploy/leadfinder-renew.sh" \
    "$APP_DIR/deploy/leadfinder-backup.sh" "$APP_DIR/deploy/postgres/init/10-leadgen.sh"
install -m 0644 "$APP_DIR/deploy/leadfinder-update.service" /etc/systemd/system/leadfinder-update.service
install -m 0644 "$APP_DIR/deploy/leadfinder-update.timer" /etc/systemd/system/leadfinder-update.timer
install -d -m 0755 /etc/systemd/system/leadfinder-update.service.d
printf '[Service]\nEnvironment=LEADFINDER_BRANCH=%s\nEnvironment=LEADFINDER_WEB=%s\n' "$BRANCH" "$WEB" \
    > /etc/systemd/system/leadfinder-update.service.d/environment.conf

# A renewal timer set up earlier (WEB_DEPLOY.md) gets the same non-root unit; its enabled state is kept.
if [ -f /etc/systemd/system/leadfinder-renew.service ]; then
    install -m 0644 "$APP_DIR/deploy/leadfinder-renew.service" /etc/systemd/system/leadfinder-renew.service
fi

# Nightly Postgres backups (deploy/leadfinder-backup.sh). The unit may write only this directory.
install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR/backups"
install -m 0644 "$APP_DIR/deploy/leadfinder-backup.service" /etc/systemd/system/leadfinder-backup.service
install -m 0644 "$APP_DIR/deploy/leadfinder-backup.timer" /etc/systemd/system/leadfinder-backup.timer
# OnFailure= target: a Telegram alert through the app's ERROR_ALERT_CHAT_ID (no secrets in the unit).
install -m 0644 "$APP_DIR/deploy/leadfinder-alert@.service" /etc/systemd/system/leadfinder-alert@.service

systemctl daemon-reload
# Backups need the self-hosted db (.env.db, DEPLOY.md). Without it the timer is installed but not enabled.
if [ -f "$APP_DIR/.env.db" ]; then
    chmod 0600 "$APP_DIR/.env.db"
    systemctl enable --now leadfinder-backup.timer
else
    echo "Нет $APP_DIR/.env.db: таймер резервных копий не включён (см. DEPLOY.md, переезд на свой Postgres)." >&2
fi
systemctl enable --now leadfinder-update.timer
systemctl start leadfinder-update.service
systemctl status leadfinder-update.timer --no-pager
