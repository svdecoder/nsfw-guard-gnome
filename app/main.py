"""
Phase 2 entrypoint: live screen monitoring.

    python3 main.py

Reads config from environment variables (all optional, sane defaults):
    OVERLAY_SOCKET_PATH   default: /data/overlay.sock
    IDLE_INTERVAL_S       default: 0.3   (near-instant first detection -
                           inference itself only costs ~200ms on 640m,
                           so polling this often is cheap given a
                           generous CPU budget; lower = faster first
                           detection, higher = less idle CPU use)
    ACTIVE_INTERVAL_S     default: 0.3
    PEEK_INTERVAL_S        default: 0.15 (monitor mode only - THIS, not
                           ACTIVE_INTERVAL_S, is what actually paces the
                           ACTIVE-state loop: each peek is a full hide
                           -> settle -> capture -> reshow cycle, so real
                           tracking responsiveness is bounded below by
                           peek_settle_s + capture+infer time, not by
                           how low you set this. ACTIVE_INTERVAL_S is
                           unused in monitor mode's active loop - it
                           only governs IDLE-state and hybrid-mode
                           polling.)
    PEEK_SETTLE_S          default: 0.02 (pause after hiding overlay,
                           before capturing, so the compositor has
                           actually redrawn without the box in frame.
                           This has a real physical floor around 1-2
                           display refresh cycles (~16-33ms at 60Hz) -
                           it is NOT possible to safely push this down
                           to ~1ms: the capture would very likely grab
                           a STALE frame that still contains our own
                           overlay box, which would make the detector
                           re-detect its own box instead of real
                           content - tracking would silently degrade
                           rather than the box "blinking faster". If
                           detections start looking stale/wrong after
                           lowering this further, that's the symptom -
                           raise it back up.)
    ENTER_THRESHOLD       default: 0.45
    SUSTAIN_THRESHOLD     default: 0.35
    EXIT_DEBOUNCE_FRAMES  default: 2     (was 4 - each debounce step is
                           one visible peek-blink while exiting, so this
                           halves the "flicker while content is going
                           away" annoyance; a fast idle re-poll after
                           exiting means a premature exit self-corrects
                           within ~IDLE_INTERVAL_S if content is still
                           actually there)
    CONFIRM_FRAMES        default: 1     (was 2 - trigger on the very
                           first strong hit instead of waiting for a
                           second consecutive one. This trades away a
                           small amount of the original single-frame
                           false-positive safety margin - see nsfw_model.py's
                           ENTER_THRESHOLD, which is the main defense
                           against spurious triggers - in exchange for
                           near-instant first coverage. Raise back to 2
                           if you'd rather have the extra confirmation.)
    MODEL_PATH            default: /app/models/640m.onnx (falls back to
                           bundled 320n if this path doesn't exist -
                           320n is faster/lighter, 640m is more accurate)
    USE_GPU               default: true  (try CUDA inference, silently
                           falls back to CPU if unavailable - see
                           nsfw_model.py docstring for why this can't
                           just be passed through NudeDetector directly)
    CAPTURE_MODE           default: monitor  ("monitor" = whole screen,
                           requires periodic peek-blinking to see behind
                           our own overlay. "window" = capture a single
                           app window you pick in GNOME's share dialog -
                           NO blinking at all, since Shell-level overlay
                           isn't composited into a window capture.
                           "hybrid" = pick a window AND the monitor in
                           the same dialog (Ctrl+click both) - fast,
                           blink-free tracking on the window, plus a
                           periodic monitor-wide scan for anything
                           outside it. See capture.py's docstring for
                           the platform limitations this works within.)
    WIDE_CHECK_INTERVAL_S  default: 10.0 (hybrid mode only - how often
                           the monitor stream is checked for content
                           outside the primary window)
    ALERT_PEEK_INTERVAL_S  default: 0.3 (hybrid mode only - once a wide
                           check finds something, how often that wide
                           box is re-peeked while it's up)
                           chrome isn't part of a window's own captured
                           surface, but only that one window is
                           protected, not the whole desktop)
    DEBUG_SNAPSHOT_DIR    default: unset (disabled). If set, saves the
                           captured frame + detection boxes to this dir
                           every time ACTIVE triggers, for visually
                           verifying box alignment against real content.
    ACTION_MODE            default: logout ("logout" = on a confirmed
                           hit, blackout the screen and log the GNOME
                           session out via gnome-session-quit. "box" =
                           original behavior, draw/track a black box
                           over the content instead of logging out.)
    GUARD_ACTIVE_START_HOUR default: 22  (local time hour the guard
                           starts actively capturing/detecting)
    GUARD_ACTIVE_END_HOUR   default: 7   (local time hour it stops;
                           start > end wraps past midnight, e.g. 22->7
                           means active 22:00-06:59. start == end means
                           always active. Outside this window the loop
                           just sleeps and rechecks the clock - no
                           capture, no inference, ~0 CPU/GPU use.)
    WINDOW_CHECK_INTERVAL_S default: 30.0 (how often to recheck the
                           clock while outside the active window)

No ports opened, no network calls at runtime. Designed to run with
`docker run --network none`.
"""

import os
import logging

from nsfw_model import NsfwModel
from capture import ScreenCapture, PortalCaptureError
from overlay_client import OverlayClient
from state_machine import GuardStateMachine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("main")


def env_float(name, default):
    return float(os.environ.get(name, default))


def env_int(name, default):
    return int(os.environ.get(name, default))


def env_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def main():
    log.info("Process starting, pid=%s", os.getpid())
    socket_path = os.environ.get("OVERLAY_SOCKET_PATH", "/data/overlay.sock")

    log.info("Loading NSFW model...")
    model = NsfwModel(
        high_conf_threshold=env_float("ENTER_THRESHOLD", 0.45),
        model_path=os.environ.get("MODEL_PATH", "/app/models/640m.onnx"),
        use_gpu=env_bool("USE_GPU", True),
    )
    log.info("Using model: %s", "640m (higher accuracy)" if model.using_640m else "320n (bundled default, faster)")
    log.info("Inference device: %s", "GPU (CUDA)" if model.gpu_active else "CPU")

    capture_mode = os.environ.get("CAPTURE_MODE", "monitor")
    log.info("Negotiating screen capture session via xdg-desktop-portal (mode=%s)...", capture_mode)
    capture = ScreenCapture(capture_mode=capture_mode)
    try:
        capture.negotiate()
        capture.start_stream()
    except PortalCaptureError as e:
        log.error("Screen capture setup failed: %s", e)
        log.error(
            "Make sure /run/user/<uid>/bus is mounted and you accepted "
            "the GNOME screen-share prompt."
        )
        raise SystemExit(1)

    overlay = OverlayClient(socket_path)

    sm = GuardStateMachine(
        model=model,
        capture=capture,
        overlay_client=overlay,
        idle_interval_s=env_float("IDLE_INTERVAL_S", 0.3),
        active_interval_s=env_float("ACTIVE_INTERVAL_S", 0.3),
        peek_interval_s=env_float("PEEK_INTERVAL_S", 0.15),
        peek_settle_s=env_float("PEEK_SETTLE_S", 0.05),
        wide_check_interval_s=env_float("WIDE_CHECK_INTERVAL_S", 10.0),
        alert_peek_interval_s=env_float("ALERT_PEEK_INTERVAL_S", 0.3),
        sustain_threshold=env_float("SUSTAIN_THRESHOLD", 0.35),
        exit_debounce_frames=env_int("EXIT_DEBOUNCE_FRAMES", 2),
        confirm_frames=env_int("CONFIRM_FRAMES", 1),
        debug_snapshot_dir=os.environ.get("DEBUG_SNAPSHOT_DIR"),
        action_mode=os.environ.get("ACTION_MODE", "logout"),
        window_start_hour=env_int("GUARD_ACTIVE_START_HOUR", 22),
        window_end_hour=env_int("GUARD_ACTIVE_END_HOUR", 7),
        window_check_interval_s=env_float("WINDOW_CHECK_INTERVAL_S", 30.0),
    )

    try:
        sm.run_forever()
    except KeyboardInterrupt:
        log.info("Shutdown signal received, pid=%s", os.getpid())
    finally:
        capture.stop()
        log.info("Shutdown complete, pid=%s, exiting", os.getpid())


if __name__ == "__main__":
    main()
