"""
Live screen monitoring — logout-only entrypoint.

    python3 main.py

Reads config from environment variables (all optional, sane defaults):
    OVERLAY_SOCKET_PATH        default: /data/overlay.sock
    IDLE_INTERVAL_S            default: 0.3
    ENTER_THRESHOLD            default: 0.45
    CONFIRM_FRAMES             default: 1
    WARN_GRACE_S               default: 5.0  (warning countdown before logout)
    MODEL_PATH                 default: /app/models/640m.onnx (falls back to
                               bundled 320n if this path doesn't exist)
    USE_GPU                    default: true
    GUARD_ACTIVE_START_HOUR    default: 22  (local time hour the guard
                               starts actively capturing/detecting)
    GUARD_ACTIVE_END_HOUR      default: 7   (local time hour it stops;
                               start > end wraps past midnight, e.g. 22->7
                               means active 22:00-06:59)
    WINDOW_CHECK_INTERVAL_S    default: 30.0

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

    log.info("Negotiating screen capture session via xdg-desktop-portal...")
    capture = ScreenCapture()
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
        confirm_frames=env_int("CONFIRM_FRAMES", 1),
        warn_grace_s=env_float("WARN_GRACE_S", 5.0),
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