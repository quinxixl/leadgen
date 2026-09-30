#!/bin/sh
set -eu

APP_DIR=${LEADFINDER_DIR:-/opt/leadfinder}

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустите установку через sudo." >&2
    exit 1
fi

chmod 0755 "$APP_DIR/deploy/leadfinder-update.sh"
install -m 0644 "$APP_DIR/deploy/leadfinder-update.service" /etc/systemd/system/leadfinder-update.service
install -m 0644 "$APP_DIR/deploy/leadfinder-update.timer" /etc/systemd/system/leadfinder-update.timer

systemctl daemon-reload
systemctl enable --now leadfinder-update.timer
systemctl start leadfinder-update.service
systemctl status leadfinder-update.timer --no-pager
