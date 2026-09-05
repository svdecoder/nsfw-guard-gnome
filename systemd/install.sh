#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SYSTEM_USER_DIR="/etc/systemd/user"
SYSTEM_DIR="/etc/systemd/system"

echo "=== nsfw-guard systemd installer ==="
echo "  Project: $PROJECT_DIR"

if [ "$(id -u)" != "0" ]; then
    echo "ERROR: Must run as root (sudo ./install.sh)"
    exit 1
fi

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found"; exit 1; }

echo "Installing system-level prebuild service..."
mkdir -p "$SYSTEM_DIR"
cat > "$SYSTEM_DIR/nsfw-guard-prebuild.service" << UNIT
[Unit]
Description=NSFW Guard prebuild Docker image
After=docker.service
Before=graphical.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/docker compose -f $PROJECT_DIR/docker-compose.yml build
WorkingDirectory=$PROJECT_DIR

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable nsfw-guard-prebuild.service

echo "Installing per-user services..."
mkdir -p "$SYSTEM_USER_DIR"

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/systemd/nsfw-guard.service.in" \
    > "$SYSTEM_USER_DIR/nsfw-guard.service"

RUN_SCRIPT="$PROJECT_DIR/systemd/run-guard.sh"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/systemd/run-guard.sh.in" \
    > "$RUN_SCRIPT"
chmod +x "$RUN_SCRIPT"

cp "$PROJECT_DIR/systemd/nsfw-guard-watchdog-a.service" "$SYSTEM_USER_DIR/"
cp "$PROJECT_DIR/systemd/nsfw-guard-watchdog-b.service" "$SYSTEM_USER_DIR/"

systemctl --global enable nsfw-guard.service
systemctl --global enable nsfw-guard-watchdog-a.service
systemctl --global enable nsfw-guard-watchdog-b.service

echo "Done. Services start on graphical login for all users."
echo "Logs: journalctl --user -u nsfw-guard.service -f"
