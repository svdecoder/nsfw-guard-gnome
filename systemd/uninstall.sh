#!/usr/bin/env bash
# systemd/uninstall.sh — removes the system-wide nsfw-guard services.
# Run as root (the top-level uninstall.sh invokes it via sudo).
set -euo pipefail

USER_DIR="/etc/systemd/user"
SYSTEM_DIR="/etc/systemd/system"

echo "Removing systemd unit files..."
rm -f "$USER_DIR/nsfw-guard.service"
rm -f "$USER_DIR/nsfw-guard-watchdog-a.service"
rm -f "$USER_DIR/nsfw-guard-watchdog-b.service"
rm -f "$SYSTEM_DIR/nsfw-guard-prebuild.service"

systemctl --global disable nsfw-guard.service 2>/dev/null || true
systemctl --global disable nsfw-guard-watchdog-a.service 2>/dev/null || true
systemctl --global disable nsfw-guard-watchdog-b.service 2>/dev/null || true

systemctl daemon-reload

echo "systemd unit files removed."