#!/usr/bin/env bash
# nsfw-guard installer — one command to set everything up.
#
#   1. Creates a default .env (your UID + data dir) if missing.
#   2. Installs + enables the GNOME Shell extension for the current user.
#   3. Installs systemd services system-wide (runs sudo) so the guard
#      starts at boot for every user, with a watchdog pair that restarts
#      it if it dies.
#   4. Builds the Docker image and starts the guard for the current user.
#
# Usage:
#   ./install.sh            # everything
#   ./install.sh --skip-build   # don't build/start the container right now
#
# Requires: docker, systemd, and (optional) gnome-extensions.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "=== nsfw-guard installer ==="
echo "  Project: $PROJECT_DIR"

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. .env
# ---------------------------------------------------------------------------

if [ -f .env ]; then
    ok "  [1/4] .env already present, leaving it untouched."
else
    cat > .env <<EOF
UID=$(id -u)
NSFW_GUARD_DATA_DIR=$HOME/.local/share/nsfw-guard
EOF
    ok "  [1/4] Created .env (you can edit values later, e.g. active hours)."
fi

# ---------------------------------------------------------------------------
# 2. GNOME Shell extension
# ---------------------------------------------------------------------------

EXT_UUID="nsfw-guard-overlay@local"
EXT_SRC="$PROJECT_DIR/gnome-extension/$EXT_UUID"
EXT_DST="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

mkdir -p "$(dirname "$EXT_DST")"
rm -rf "$EXT_DST"
cp -r "$EXT_SRC" "$EXT_DST"
ok "  [2/4] Extension copied to $EXT_DST"

if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions enable "$EXT_UUID" >/dev/null 2>&1 || true
    ok "  [2/4] Extension enabled."
    warn "  NOTE: if this is a fresh install, log out and back in once"
    warn "        so GNOME Shell loads the extension."
else
    warn "  [2/4] 'gnome-extensions' not found — enable it with:"
    warn "        gnome-extensions enable $EXT_UUID"
fi

# ---------------------------------------------------------------------------
# 3. systemd services (system-wide, needs sudo)
# ---------------------------------------------------------------------------

echo "  [3/4] Installing systemd services (system-wide)..."
if [ "$(id -u)" = "0" ]; then
    "$PROJECT_DIR/systemd/install.sh"
else
    if ! sudo -n true 2>/dev/null; then
        warn "  sudo is needed for system-wide service install."
    fi
    sudo "$PROJECT_DIR/systemd/install.sh"
fi
ok "  [3/4] systemd services installed."

# ---------------------------------------------------------------------------
# 4. Build image + start guard for the current user
# ---------------------------------------------------------------------------

if [ "${1:-}" = "--skip-build" ]; then
    warn "  [4/4] Skipping build/start (--skip-build)."
    ok "Done. Run 'sudo systemctl start nsfw-guard-prebuild.service' then"
    ok "      'systemctl --user start nsfw-guard.service' when ready."
    exit 0
fi

echo "  [4/4] Building Docker image (first build downloads a lot)..."
if [ "$(id -u)" = "0" ]; then
    systemctl start nsfw-guard-prebuild.service
else
    if ! sudo -n true 2>/dev/null; then
        warn "  sudo needed to start the nsfw-guard-prebuild.service."
    fi
    sudo systemctl start nsfw-guard-prebuild.service
fi
ok "  [4/4] Image built."

echo "  [4/4] Starting the guard for this session..."
systemctl --user start nsfw-guard.service 2>/dev/null || \
    warn "  Could not start nsfw-guard via systemd --user (are you in a graphical session?)."

# ---------------------------------------------------------------------------

echo ""
echo "=== Installation complete ==="
echo ""
echo "The guard backs every user's start at boot and is kept alive by a"
echo "mutual watchdog pair (restart within ~15s if killed)."
echo ""
echo "To see logs:   journalctl --user -u nsfw-guard.service -f"
echo "To stop:       systemctl --user stop nsfw-guard.service"
echo "To uninstall:  ./uninstall.sh"
echo ""
echo "On the first run, GNOME will show a 'Share your screen?' prompt —"
echo "accept it. Detection runs from 22:00 to 07:00 by default; edit"
echo "GUARD_ACTIVE_START_HOUR / GUARD_ACTIVE_END_HOUR in .env to change."