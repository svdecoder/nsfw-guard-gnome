#!/usr/bin/env bash
# nsfw-guard uninstaller — removes everything install.sh set up.
#
#   1. Stops and disables the systemd services (system-wide, needs sudo).
#   2. Removes the GNOME Shell extension for the current user.
#   3. Optionally removes the Docker image and local data.
#
# Usage:
#   ./uninstall.sh               # services + extension
#   ./uninstall.sh --purge       # also delete Docker image and guard data
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_UUID="nsfw-guard-overlay@local"
EXT_DST="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }

echo "=== nsfw-guard uninstaller ==="

# ---------------------------------------------------------------------------
# 1. systemd services
# ---------------------------------------------------------------------------

echo "[1/3] Stopping and disabling systemd services..."
systemctl --user stop nsfw-guard.service 2>/dev/null || true
systemctl --user stop nsfw-guard-watchdog-a.service 2>/dev/null || true
systemctl --user stop nsfw-guard-watchdog-b.service 2>/dev/null || true

systemctl --global disable nsfw-guard.service 2>/dev/null || true
systemctl --global disable nsfw-guard-watchdog-a.service 2>/dev/null || true
systemctl --global disable nsfw-guard-watchdog-b.service 2>/dev/null || true

if [ "$(id -u)" = "0" ]; then
    "$PROJECT_DIR/systemd/uninstall.sh"
else
    if ! sudo -n true 2>/dev/null; then
        warn "  sudo is needed to remove system-wide service files."
    fi
    sudo "$PROJECT_DIR/systemd/uninstall.sh"
fi
ok "  [1/3] systemd services removed."

# ---------------------------------------------------------------------------
# 2. GNOME Shell extension
# ---------------------------------------------------------------------------

echo "[2/3] Removing GNOME Shell extension..."
if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions disable "$EXT_UUID" >/dev/null 2>&1 || true
fi
rm -rf "$EXT_DST"
ok "  [2/3] Extension removed."

# ---------------------------------------------------------------------------
# 3. Optional purge: docker image + local data
# ---------------------------------------------------------------------------

if [ "${1:-}" = "--purge" ]; then
    echo "[3/3] Purging Docker image and local data..."
    docker rmi -f nsfw-guard:dev >/dev/null 2>&1 || true
    rm -rf "$HOME/.local/share/nsfw-guard"
    ok "  [3/3] Purged."
else
    echo "[3/3] Skipping Docker image/data removal (re-run with --purge to remove)."
    echo "      Data dir kept: $HOME/.local/share/nsfw-guard"
    echo "      Image kept:    nsfw-guard:dev"
fi

echo ""
echo "=== Uninstall complete ==="