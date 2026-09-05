# nsfw-guard

A fully local, offline NSFW screen-content guard for GNOME/Wayland Linux.

It watches the screen using a `--network none` Docker container and a local
NudeNet ONNX model. When explicit content is confirmed **during a
configured time window** (default 22:00–07:00), it:

1. shows a full-screen warning + a CRITICAL system notification with a
   countdown, and
2. if the content is still on screen when the grace period (5 s) expires,
   logs your GNOME session out.

Closing the content during the grace period cancels the logout.

No network calls at runtime, no cloud service, no telemetry. Everything
runs on your own GPU/CPU inside a sandboxed container.

---

## How it works

```
┌──────────────────────────────────────────┐
│ CONTAINER  (docker, --network none)       │
│                                            │
│  xdg-desktop-portal + PipeWire capture     │
│              │                             │
│              ▼                             │
│  NudeNet (ONNX, local) -> IDLE→WARNING→    │
│  LOGOUT state machine, gated to your       │
│  active hours                              │
│              │                             │
│              ▼                             │
│      Unix socket (bind-mounted, local IPC) │
└──────────────────┬─────────────────────────┘
                   │
                   ▼
     ┌──────────────────────────────┐
     │  GNOME Shell extension       │
     │  - full-screen warning       │
     │  - CRITICAL notification     │
     │  - blackout + gnome-session- │
     │    quit --logout --no-prompt │
     └──────────────────────────────┘
```

Two parts, because a sandboxed container has no supported way to draw an
always-on-top overlay or end a GNOME session directly:

- **The container** does all the sensitive work — screen capture,
  inference, thresholds, the active-hours clock, the decision to warn and
  to log out. Network is disabled (`network_mode: "none"`); it talks to
  the host only through a bind-mounted Unix socket.
- **The GNOME Shell extension** is intentionally dumb. It listens on that
  socket for three messages:

  | message | effect |
  |---|---|
  | `{"action": "warn", "seconds": N}` | full-screen warning overlay + CRITICAL notification with countdown |
  | `{"action": "clear_warn"}` | dismisses the warning |
  | `{"action": "logout"}` | blacks out every monitor immediately, then runs `gnome-session-quit --logout --no-prompt` |

### Detection flow

```
IDLE ──(confirmed NSFW hit)──> WARNING (warn + countdown)
                                 │
                    within grace  │            grace expires
                 │                │ content still there │
                 ▼                ▼                     ▼
          content cleared ──> IDLE                  LOGOUT
          (warning cancelled)
```

- **IDLE** polls frames at `IDLE_INTERVAL_S`. Only `CONFIRM_FRAMES`
  consecutive high-confidence hits (default 1) start a warning.
- **WARNING** keeps capturing during `WARN_GRACE_S` (default 5 s). If the
  content disappears for `CONFIRM_FRAMES` frames, the warning is
  cancelled and the machine returns to IDLE. If the grace period expires
  *and* the content is still present, it logs out.
- **LOGOUT** sends the final message. The extension blacks the screen and
  ends the session; the container then exits on its own once the portal /
  D-Bus session is gone.

---

## Prerequisites

- **Docker** with `docker compose` (v2)
- **GNOME on Wayland**, GNOME Shell 45–50
- `xdg-desktop-portal` + `xdg-desktop-portal-gnome` (stock on GNOME)
- `systemd` (user + system)
- Optional but recommended: `nvidia-container-toolkit` for GPU inference
  (falls back to CPU automatically)

---

## Install

Quick install (`.env`, GNOME extension, systemd services, Docker build):

```bash
./install.sh
```

This:

1. creates a default `.env` (`UID` + data dir) if none exists,
2. copies the extension to
   `~/.local/share/gnome-shell/extensions/` and enables it,
3. installs systemd services **system-wide** (asks for `sudo`), and
4. builds the Docker image and starts the guard for your session.

On a fresh install, log out and back in **once** so GNOME Shell loads the
extension.

Uninstall:

```bash
./uninstall.sh            # services + extension
./uninstall.sh --purge    # also delete the Docker image and local data
```

### Manual install

```bash
# .env
printf "UID=%s\nNSFW_GUARD_DATA_DIR=%s\n" "$(id -u)" "$HOME/.local/share/nsfw-guard" > .env

# GNOME extension
mkdir -p ~/.local/share/gnome-shell/extensions
cp -r gnome-extension/nsfw-guard-overlay@local ~/.local/share/gnome-shell/extensions/
gnome-extensions enable nsfw-guard-overlay@local

# systemd (all users, starts at boot)
sudo ./systemd/install.sh

# build + start for the current session
sudo systemctl start nsfw-guard-prebuild.service
systemctl --user start nsfw-guard.service
```

### What the systemd setup does

Three things run on every GNOME login for every user, plus one at boot:

| unit | where | role |
|---|---|---|
| `nsfw-guard-prebuild.service` | system | one-shot at boot: builds the Docker image |
| `nsfw-guard.service` | user (global) | runs the guard container in the foreground; `Restart=always` |
| `nsfw-guard-watchdog-a.service` | user (global) | every 15 s restarts `nsfw-guard` or `watchdog-b` if dead |
| `nsfw-guard-watchdog-b.service` | user (global) | every 15 s restarts `nsfw-guard` or `watchdog-a` if dead |

The two watchdogs supervise each other *and* the main guard, so if any
one of the three is killed the remaining units bring it back within
~15 s. The container itself runs with `restart: "no"` on purpose —
systemd is the single restart authority, which makes the whole stack
recover cleanly across logouts (the guard exits when the session ends
and is restarted on the next graphical login).

Logs:

```bash
journalctl --user -u nsfw-guard.service -f          # guard
journalctl --user -u nsfw-guard-watchdog-a.service  # watchdog activity
```

### Getting the 640m model (recommended)

The container ships with NudeNet's small bundled model (`320n`,
320×320), which is noticeably less accurate than the larger `640m`
(640×640) model — particularly on smaller/harder regions. `640m` can't
be auto-downloaded during `docker build` because GitHub gates that
release asset behind a login redirect.

One-time manual step:

1. Download `640m.onnx` in a normal browser:
   https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/640m.onnx
   (if that redirects to a login, try the Hugging Face mirror:
   https://huggingface.co/spaces/Nymbo/Nudity-Censor/resolve/main/640m.onnx)
2. Place it in the project:
   ```bash
   mkdir -p models
   mv ~/Downloads/640m.onnx models/
   ```

`docker-compose.yml` bind-mounts `./models` read-only into the
container. If you skip this step the guard still works — it silently
falls back to `320n` and logs which model it's using at startup.

---

## Configuration

All configuration lives in `.env` (read automatically by `docker
compose`) or as `environment:` entries in `docker-compose.yml`. The full
authoritative list with rationale is in `app/main.py`'s docstring.

| Variable | Default | What it does |
|---|---|---|
| `GUARD_ACTIVE_START_HOUR` | `22` | Local-time hour (24 h) the guard starts capturing/detecting. |
| `GUARD_ACTIVE_END_HOUR` | `7` | Local-time hour it stops. `start > end` wraps past midnight (22→7 = active 22:00 through 06:59). Set both equal to run 24/7. |
| `WARN_GRACE_S` | `5.0` | Seconds between a confirmed hit and the forced logout. Content closed within that window cancels the logout. |
| `ENTER_THRESHOLD` | `0.45` | Confidence needed for a hit to count. |
| `CONFIRM_FRAMES` | `1` | Consecutive high-confidence hits required before the warning starts. |
| `IDLE_INTERVAL_S` | `0` | Polling pace in IDLE. `0` = run as fast as capture+inference allows. |
| `USE_GPU` | `true` | Try CUDA inference; falls back to CPU silently if unavailable. |
| `MODEL_PATH` | `/app/models/640m.onnx` | Falls back to bundled `320n` if this path doesn't exist. |
| `WINDOW_CHECK_INTERVAL_S` | `30.0` | How often to recheck the clock while outside the active window (no capture / no inference there). |

Trigger classes live in `app/nsfw_model.py` (`TRIGGER_LABELS` —
deliberately restricted to unambiguous explicit classes to keep false
positives near zero). Changing them requires a rebuild
(`systemctl --user restart nsfw-guard.service`).

---

## Updating the GNOME extension after a code change

The extension lives outside the container at
`~/.local/share/gnome-shell/extensions/nsfw-guard-overlay@local/`.
Editing the copy here does nothing until you copy it over and reload:

```bash
cp gnome-extension/nsfw-guard-overlay@local/extension.js \
   ~/.local/share/gnome-shell/extensions/nsfw-guard-overlay@local/extension.js

gnome-extensions disable nsfw-guard-overlay@local
gnome-extensions enable nsfw-guard-overlay@local
```

Watch `journalctl --user -f -o cat | grep nsfw-guard` while doing this —
you should see a fresh `listening` line. If it doesn't appear, a full
logout/login guarantees a reload. Then restart the container so it
reconnects to the new extension instance:

```bash
systemctl --user restart nsfw-guard.service
```

---

## Verifying end-to-end

1. Temporarily set `GUARD_ACTIVE_START_HOUR`/`GUARD_ACTIVE_END_HOUR` to
   cover the current time (or set both equal for 24/7).
2. Watch the logs: `journalctl --user -u nsfw-guard.service -f`
3. Open explicit content on screen. In order you should see:
   ```
   TRIGGER (entering WARNING): FEMALE_BREAST_EXPOSED(0.68)
   WARNING state entered — 5.0s grace period started
   Grace period expired — logging out session
   ```
   the full-screen warning + notification, then a blackout and a real
   logout. If you close the content within the grace period you should
   instead see `Content cleared during grace period — cancelling warning`
   and stay logged in.
4. Set the hours back to your real schedule afterward.

---

## Running the tests

The state-machine logic is unit-tested offline (no screen, no Docker):

```bash
python3 app/test_state_machine.py
```

---

## Troubleshooting

**No screen-share dialog / times out on first run**
Confirm `xdg-desktop-portal-gnome` is running
(`systemctl --user status xdg-desktop-portal-gnome`) and that
`XDG_CURRENT_DESKTOP` is set. Without it the portal doesn't know which
backend to use and the call hangs.

**Logs show `TRIGGER` but nothing happens on screen**
The extension GNOME Shell loaded doesn't have the current handler —
re-run `./install.sh` or re-copy `extension.js` and reload (see
[above](#updating-the-gnome-extension-after-a-code-change)). Confirm
with:
```bash
grep -n "_triggerLogout" ~/.local/share/gnome-shell/extensions/*/extension.js
```

**`gnome-session-quit` errors in the shell log**
Check the binary exists on the host (`which gnome-session-quit`) — it
ships with `gnome-session`.

**Guard never triggers during the active window**
`journalctl --user -u nsfw-guard.service` should show the startup line
`Starting guard loop (active window 22:00-07:00 local, action=logout, ...)`.
If the hours look wrong, fix `.env` and restart the service. Check the
container clock with `docker compose exec nsfw-guard date`.

**GPU not used (`Inference device: CPU`)**
Requires `nvidia-container-toolkit` on the host and the `deploy:` block
in `docker-compose.yml` (present by default — remove it if you don't
have an NVIDIA GPU; the app falls back to CPU automatically).

**Restore-token / consent dialog reappears every run**
The portal "share" permission is persisted in
`~/.local/share/nsfw-guard/state/restore_token.txt`. If that directory
isn't writable/mounted, you'll be asked each run — check
`NSFW_GUARD_DATA_DIR` in `.env`.

---

## Design notes / known limitations

- **The consequence of a hit is a forced logout**, so the trigger set is
  deliberately conservative: only unambiguous exposed-content classes
  count (`FEMALE_GENITALIA_EXPOSED`, `MALE_GENITALIA_EXPOSED`,
  `FEMALE_BREAST_EXPOSED`, `BUTTOCKS_EXPOSED`, `ANUS_EXPOSED`).
  Covered/borderline classes (lingerie, swimwear, etc.) are excluded to
  keep false positives near zero, and the 5 s warning grace lets a
  momentary mis-detection be dismissed before anything happens.
- **Logging out ends the container's own portal/D-Bus session too** — the
  container is expected to exit shortly after a logout fires. systemd
  brings it back on the next graphical login.
- **No exposed ports, no runtime network calls** — `network_mode: "none"`;
  all container↔host communication is a bind-mounted Unix socket.
- **Monitor-wide capture only** — earlier `window`/`hybrid` capture modes
  and the black-box overlay ("censoring") behavior were removed in favor
  of the single logout-only path.