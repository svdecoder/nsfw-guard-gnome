"""
Standalone capture sanity check - Phase 2 hardware validation.

Exercises ONLY the xdg-desktop-portal + PipeWire path (capture.py),
completely bypassing the detector and state machine. Dumps N raw
captured frames to PNG so you can visually confirm:
  - the portal negotiation actually completes (consent dialog behavior,
    restore token persistence across runs)
  - frames arrive at all, at the expected resolution
  - color channels are correct (RGB, not swapped/corrupted) - frames
    are saved as-is (RGB) via cv2 with an explicit channel flip, since
    cv2.imwrite expects BGR
  - frame timing / stall behavior

This does NOT test detection - just capture. Run detect.py separately
for that. Keeping these isolated makes it obvious which layer is at
fault if something goes wrong.

Run with (same mounts as Phase 2's main.py, see README):
  docker run --rm --network none \\
    -e XDG_RUNTIME_DIR=/run/user/$(id -u) \\
    -e XDG_CURRENT_DESKTOP=$XDG_CURRENT_DESKTOP \\
    -v /run/user/$(id -u)/bus:/run/user/$(id -u)/bus \\
    -v ~/.local/share/nsfw-guard:/data \\
    --entrypoint python \\
    nsfw-guard:dev test_capture.py

Optional args:
  --frames N     number of frames to capture (default 5)
  --interval S   seconds to sleep between grabs (default 1.0)
  --out DIR      output directory inside the container (default /data/capture_test)
"""

import argparse
import sys
import time
from pathlib import Path

import cv2

from capture import ScreenCapture, PortalCaptureError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=5)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--out", type=str, default="/data/capture_test")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Negotiating with xdg-desktop-portal...")
    print("(first run: watch for GNOME's 'Share your screen?' dialog - accept it)")
    cap = ScreenCapture()
    t0 = time.time()
    try:
        cap.negotiate(timeout_s=60)
    except PortalCaptureError as e:
        print(f"[FAIL] Portal negotiation failed: {e}")
        sys.exit(1)
    print(f"Negotiation done in {time.time() - t0:.1f}s (node_id={cap._node_id})")

    print("Starting PipeWire stream...")
    try:
        cap.start_stream()
    except Exception as e:
        print(f"[FAIL] Could not start GStreamer pipeline: {e}")
        sys.exit(1)

    # give the pipeline a moment to reach PLAYING and produce a first buffer
    time.sleep(0.5)

    ok_count = 0
    for i in range(args.frames):
        t0 = time.time()
        frame = cap.grab_frame(timeout_s=5.0)
        elapsed = time.time() - t0

        if frame is None:
            print(f"  [{i}] [FAIL] no frame received within timeout ({elapsed:.1f}s)")
        else:
            h, w, c = frame.shape
            out_path = out_dir / f"frame_{i:02d}.png"
            # frame is RGB (per capture.py's pipeline caps); cv2.imwrite
            # expects BGR, so flip channels only for this debug dump -
            # do NOT change this in nsfw_model.py, which expects RGB in.
            cv2.imwrite(str(out_path), frame[:, :, ::-1])
            print(f"  [{i}] [OK] {w}x{h}x{c}  ({elapsed*1000:.0f}ms)  -> {out_path}")
            ok_count += 1

        if i < args.frames - 1:
            time.sleep(args.interval)

    cap.stop()

    print(f"\n{ok_count}/{args.frames} frames captured successfully.")
    print(f"Inspect PNGs in {out_dir} (bind-mounted to your host's ~/.local/share/nsfw-guard/capture_test).")
    if ok_count < args.frames:
        print("Some frames failed - check stride/format assumptions in capture.py's grab_frame() "
              "if images look corrupted, or investigate PipeWire node stability if frames were "
              "missing entirely.")
        sys.exit(1)


if __name__ == "__main__":
    main()
