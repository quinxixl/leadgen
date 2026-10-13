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

chmod 0755 "$APP_DIR/deploy/leadfinder-update.sh"
install -m 0644 "$APP_DIR/deploy/leadfinder-update.service" /etc/systemd/system/leadfinder-update.service
install -m 0644 "$APP_DIR/deploy/leadfinder-update.timer" /etc/systemd/system/leadfinder-update.timer
install -d -m 0755 /etc/systemd/system/leadfinder-update.service.d
printf '[Service]\nEnvironment=LEADFINDER_BRANCH=%s\nEnvironment=LEADFINDER_WEB=%s\n' "$BRANCH" "$WEB" \
    > /etc/systemd/system/leadfinder-update.service.d/environment.conf

systemctl daemon-reload
systemctl enable --now leadfinder-update.timer
systemctl start leadfinder-update.service
systemctl status leadfinder-update.timer --no-pager
