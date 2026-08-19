# nsfw-guard

A fully local, offline NSFW screen-content guard for GNOME/Wayland Linux.
It watches your screen, and if it detects explicit content **during a
configured time window** (default: 22:00–07:00), it logs your session
out — dropping you back to the GNOME login screen.

No network calls at runtime, no cloud service, no telemetry. Detection
runs entirely inside a `--network none` Docker container on your own
GPU/CPU.

---

## How it works

```
┌───────────────────────────────────────────┐
│ CONTAINER  (docker, --network none)        │
│                                             │
│  xdg-desktop-portal + PipeWire capture     │
│              │                             │
│              ▼                             │
│  NudeNet (ONNX, local) → IDLE/ACTIVE        │
│  state machine, gated to your active hours │
│              │                             │
│              ▼                             │
│      Unix socket (bind-mounted, local IPC) │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
     ┌───────────────────────────────┐
     │  GNOME Shell extension (host)  │
     │  - blacks out every monitor    │
     │  - runs gnome-session-quit     │
     │    --logout --no-prompt        │
     └───────────────────────────────┘
```

Two parts, because a sandboxed container has no supported way to draw
an always-on-top overlay or end a GNOME session directly:

- **The container** does all the sensitive work — screen capture,
  inference, thresholds, the active-hours clock, the decision to act.
  It never touches the network, never opens a port, and only writes to
  a Unix socket.
- **The GNOME Shell extension** is intentionally dumb — it has no
  detection logic. It listens on that socket for three possible
  messages and reacts:

  | message | effect |
  |---|---|
  | `{"action": "show", "boxes": [...]}` | draws black rectangles at the given coordinates |
  | `{"action": "clear"}` | removes any rectangles |
  | `{"action": "logout"}` | blacks out every monitor immediately, then runs `gnome-session-quit --logout --no-prompt` |

By default the guard runs in **logout mode**: any confirmed hit inside
your active-hours window blacks the screen and ends the session, no
tracking box involved. The original black-box mode (`ACTION_MODE=box`)
still exists if you'd rather it just censor the region live instead.

---

## Prerequisites

- Docker (with `docker compose`)
- GNOME on Wayland
- `xdg-desktop-portal` + `xdg-desktop-portal-gnome` (present on any
  stock GNOME install)
- Optional but recommended: `nvidia-container-toolkit`, if you want GPU
  inference (falls back to CPU automatically if unavailable/untoolkitted)

---

## Setup

### 1. One-time `.env`

```bash
cd nsfw-guard
printf "UID=%s\nNSFW_GUARD_DATA_DIR=%s\n" "$(id -u)" "$HOME/.local/share/nsfw-guard" > .env
```

Then add/adjust the guard's behavior in the same `.env` file (see
[Configuration](#configuration) below) — at minimum you'll usually want
nothing, since the defaults are already logout mode, 22:00–07:00.

### 2. Get the 640m model (recommended)

The container ships with NudeNet's small bundled model (`320n`,
320×320), which is noticeably less accurate than the larger `640m`
(640×640) model — particularly on smaller/harder regions. `640m`
**can't be auto-downloaded** during `docker build`: GitHub gates that
specific release asset behind a login redirect even though the repo is
public.

One-time manual step:

1. Download it in a normal browser:
   https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/640m.onnx

   If that redirects to a login page, try the Hugging Face mirror:
   https://huggingface.co/spaces/Nymbo/Nudity-Censor/resolve/main/640m.onnx

2. Place it in the project:

   ```bash
   mkdir -p models
   mv ~/Downloads/640m.onnx models/
   ```

The compose file already bind-mounts `./models` read-only into the
container. If you skip this step, the guard still works — it silently
falls back to `320n`, and logs which model it's actually using on
startup:

```
Using model: 640m (higher accuracy)   # or: 320n (bundled default, faster)
```

### 3. Install the GNOME Shell extension

```bash
mkdir -p ~/.local/share/gnome-shell/extensions
cp -r gnome-extension/nsfw-guard-overlay@local \
  ~/.local/share/gnome-shell/extensions/

gnome-extensions enable nsfw-guard-overlay@local
```

On a first-ever install on Wayland, log out and back in once to make
GNOME Shell pick it up. Confirm it's running:

```bash
gnome-extensions info nsfw-guard-overlay@local   # should show State: ENABLED
journalctl --user -f -o cat | grep nsfw-guard    # should show "listening on ..."
```

### 4. Run

```bash
docker compose up -d --build
```

`Ctrl+C` (if run in the foreground) or `docker compose down` stops it.
`up -d` runs it detached; `docker compose restart` bounces it after a
config change.

**First run only:** GNOME will show a "Share your screen?" dialog —
accept it. A restore token gets saved to
`~/.local/share/nsfw-guard/state/restore_token.txt` so you won't be
asked again on later runs.

---

## Configuration

All of this lives in `.env` (read automatically by `docker compose`)
or can be set directly as `environment:` entries in
`docker-compose.yml`. Full authoritative list with in-depth rationale
is in `app/main.py`'s docstring — this is the practical summary.

### Action & scheduling

| Variable | Default | What it does |
|---|---|---|
| `ACTION_MODE` | `logout` | `logout` = blackout + end the GNOME session on a confirmed hit. `box` = original behavior, draw/track a black box over the content instead. |
| `GUARD_ACTIVE_START_HOUR` | `22` | Local-time hour (24h) the guard starts actively capturing/detecting. |
| `GUARD_ACTIVE_END_HOUR` | `7` | Local-time hour it stops. `start > end` wraps past midnight (22→7 means active 22:00 through 06:59). Set both equal to run 24/7. |
| `WINDOW_CHECK_INTERVAL_S` | `30.0` | How often to recheck the clock while outside the active window. Outside the window there's no capture and no inference at all — effectively 0% CPU/GPU use. |

### Capture & detection

| Variable | Default | What it does |
|---|---|---|
| `CAPTURE_MODE` | `monitor` | `monitor` = whole screen (needs brief periodic "peek" blinks to see behind its own overlay, only relevant in `box` mode). `window` = a single app window, no blinking, but only that window is covered. `hybrid` = a window + the monitor together — fast blink-free tracking on the window, slow periodic wide scan of the rest of the screen. |
| `USE_GPU` | `true` | Try CUDA inference, silently falls back to CPU if unavailable. |
| `MODEL_PATH` | `/app/models/640m.onnx` | Falls back to bundled `320n` if this path doesn't exist. |
| `ENTER_THRESHOLD` | `0.45` | Confidence needed to trigger from idle. |
| `SUSTAIN_THRESHOLD` | `0.35` | Confidence needed to keep an already-active detection alive (`box` mode only). |
| `CONFIRM_FRAMES` | `1` | Consecutive high-confidence hits required before triggering. |
| `EXIT_DEBOUNCE_FRAMES` | `2` | Consecutive clears required to drop out of `box` mode's active state. |
| `IDLE_INTERVAL_S` / `ACTIVE_INTERVAL_S` | `0` | Polling pace; `0` = run as fast as capture+inference allows. |
| `PEEK_INTERVAL_S` / `PEEK_SETTLE_S` | `0.15` / `0.02` | `box`+`monitor` mode only — pacing of the hide/capture/redraw peek cycle. |
| `DEBUG_SNAPSHOT_DIR` | unset | If set, saves the captured frame + detection boxes to this dir on every trigger, for visual verification. |

Trigger classes and thresholds live in `app/nsfw_model.py`
(`TRIGGER_LABELS` — deliberately restricted to unambiguous explicit
classes, excluding covered/borderline ones, to keep false positives
near zero). Editing that file requires a rebuild
(`docker compose up --build`) to take effect.

### Example `.env`

```bash
UID=1000
NSFW_GUARD_DATA_DIR=/home/youruser/.local/share/nsfw-guard

CAPTURE_MODE=monitor
USE_GPU=true

ACTION_MODE=logout
GUARD_ACTIVE_START_HOUR=22
GUARD_ACTIVE_END_HOUR=7
```

---

## Updating the GNOME extension after a code change

The extension file lives outside the container, at
`~/.local/share/gnome-shell/extensions/nsfw-guard-overlay@local/`.
Editing the copy in this repo does nothing until you copy it over and
reload:

```bash
cp gnome-extension/nsfw-guard-overlay@local/extension.js \
   ~/.local/share/gnome-shell/extensions/nsfw-guard-overlay@local/extension.js

gnome-extensions disable nsfw-guard-overlay@local
gnome-extensions enable nsfw-guard-overlay@local
```

Watch `journalctl --user -f -o cat | grep nsfw-guard` while doing this
— you should see a fresh `listening on ...` line. If disable/enable
doesn't produce one (Wayland shell sometimes caches the extension
module stubbornly), a full logout/login guarantees a reload.

After reloading the extension, restart the container too — it may be
holding a socket connection to the previous extension instance:

```bash
docker compose restart
```

---

## Verifying it actually works end-to-end

1. Temporarily set `GUARD_ACTIVE_START_HOUR`/`GUARD_ACTIVE_END_HOUR` to
   cover the current time (or leave them equal for 24/7) so you don't
   have to wait for 22:00 to test.
2. `docker compose up --build` and watch the logs.
3. Open a test image/clip from your `test_images/` set (see Phase 1
   testing below) that you already know triggers detection.
4. You should see, in order:
   ```
   TRIGGER: FEMALE_BREAST_EXPOSED(0.6x)
   NSFW content confirmed during active window -> logging out session
   ```
   and — if the extension is correctly wired — the screen should black
   out within a moment, followed by an actual GNOME logout back to the
   login screen.
5. If the container logs show `TRIGGER`/`logging out` repeatedly but
   the screen never blacks out or the session never ends, the
   extension isn't receiving/handling the message — see
   [Troubleshooting](#troubleshooting).

Set the hours back to your real schedule afterward.

---

## Testing detection in isolation (no live capture)

Useful for tuning thresholds without needing the full pipeline running.

1. Drop a mix of test images into `test_images/` — some genuinely
   explicit, some deliberately borderline (swimwear, art nudes, beach/
   workout photos), and some totally unrelated, so you can judge both
   false positives and false negatives.
2. Run:
   ```bash
   docker run --rm --network none \
     -v "$(pwd)/test_images:/data/test_images:ro" \
     -v "$(pwd)/test_output:/data/test_output" \
     -v "$(pwd)/models:/app/models:ro" \
     --entrypoint python \
     nsfw-guard:dev detect.py
   ```
3. Each image prints `[TRIGGER] filename: LABEL(score) (Nms)` (censored
   copy written to `test_output/`) or `[clear] filename (Nms)`.
4. Adjust `ENTER_THRESHOLD`/`TRIGGER_LABELS` in `app/nsfw_model.py` and
   rebuild to retune.

---

## Troubleshooting

**No screen-share dialog appears / times out on first run**
Confirm `xdg-desktop-portal-gnome` is running:
`systemctl --user status xdg-desktop-portal-gnome`. Also confirm
`XDG_CURRENT_DESKTOP` is set in your shell before running compose —
without it the portal doesn't know which backend to hand off to, and
the call hangs silently.

**Container logs show `TRIGGER` / `logging out` but nothing happens on screen**
The extension currently loaded by GNOME Shell doesn't have the
`logout` handler — almost always means the updated `extension.js`
wasn't copied to
`~/.local/share/gnome-shell/extensions/nsfw-guard-overlay@local/`, or
was copied but not reloaded. See
[Updating the GNOME extension](#updating-the-gnome-extension-after-a-code-change)
above. Confirm with:
```bash
grep -n "_triggerLogout" ~/.local/share/gnome-shell/extensions/nsfw-guard-overlay@local/extension.js
```
If that prints nothing, the file on disk is stale.

**`gnome-session-quit` errors in the shell log**
Check the binary is actually present (`which gnome-session-quit` on
the host, not in the container) — it ships with `gnome-session`,
which should already be installed on a stock GNOME desktop.

**Guard never triggers even during the active window**
Check `docker compose logs -f` for the startup line confirming the
window: `Starting guard loop in IDLE state (active window 22:00-07:00
local, action=logout)`. If the hours look wrong, double check
`GUARD_ACTIVE_START_HOUR`/`GUARD_ACTIVE_END_HOUR` in `.env` and that
you restarted the container after changing it. Also confirm the
container's clock/timezone matches what you expect — `docker compose
exec nsfw-guard date` (while it's running) shows what the container
thinks "now" is.

**GPU not being used (`Inference device: CPU` when you expected GPU)**
Requires `nvidia-container-toolkit` on the host and the `deploy:`
block in `docker-compose.yml` (present by default — remove it if you
don't have an NVIDIA GPU, the app falls back to CPU automatically
either way).

**Restore-token / consent dialog reappears every run**
Confirms `~/.local/share/nsfw-guard` is actually bind-mounted and
writable — check `NSFW_GUARD_DATA_DIR` in `.env` points somewhere real
and the container isn't failing to write
`state/restore_token.txt` there.

**Frames look garbled/wrong colors in `DEBUG_SNAPSHOT_DIR` output**
There's a manual BGR/RGB channel-order conversion in `nsfw_model.py`
that assumes a specific order out of the GStreamer pipeline — if it's
visibly wrong, that's worth digging into since it silently degrades
detection accuracy rather than crashing.

---

## Design notes / known limitations

- **`CAPTURE_MODE=monitor` (default) uses a "peek" pattern to avoid
  the compositor's own overlay contaminating captured frames** — this
  only matters in `box` mode. In `logout` mode there's no ongoing box
  to occlude, so this only affects the brief moment before a session
  ends.
- **`window` and `hybrid` modes** cover a single app window (plus,
  in hybrid, a slow periodic scan of the rest of the screen) rather
  than the whole desktop at full speed. Good trade-off if you mostly
  want tight, blink-free coverage on one browser/video window.
- **Trigger labels are deliberately conservative** (`nsfw_model.py`) —
  only unambiguous exposed-content classes count, covered/borderline
  classes (lingerie, swimwear, etc.) are excluded on purpose to keep
  false positives near zero, since the consequence here (a forced
  logout) is more disruptive than a censoring box.
- **Logging out ends the container's own portal/D-Bus session too** —
  expect the container to exit shortly after a logout fires
  (`restart: unless-stopped` in `docker-compose.yml` brings it back up
  automatically once you log back in during the active window).
- **No exposed ports, no runtime network calls** — the container runs
  with `network_mode: "none"`; all container↔host communication is a
  bind-mounted Unix socket.
