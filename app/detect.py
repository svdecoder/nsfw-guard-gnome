"""
Phase 1 entrypoint.

Runs the NSFW detector against every image in /data/test_images,
prints detections, and writes an annotated copy (black boxes over
triggered regions) to /data/test_output so you can visually verify
false-positive / false-negative behavior before we wire up live capture.

No network access needed at runtime - model weights are baked into
the image at build time by the `nudenet` package's own installer.
"""

import sys
import time
from pathlib import Path

import cv2

from nsfw_model import NsfwModel

INPUT_DIR = Path("/data/test_images")
OUTPUT_DIR = Path("/data/test_output")


def censor_image(image_path: Path, detections, output_path: Path):
    img = cv2.imread(str(image_path))
    if img is None:
        print(f"  [!] could not read image for annotation: {image_path}")
        return
    for det in detections:
        x, y, w, h = [int(v) for v in det.box]
        cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 0), thickness=-1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), img)


def main():
    if not INPUT_DIR.exists() or not any(INPUT_DIR.iterdir()):
        print(f"No images found in {INPUT_DIR}. Mount test images there and re-run.")
        sys.exit(1)

    print("Loading model...")
    t0 = time.time()
    model = NsfwModel(high_conf_threshold=0.45)
    model_name = "640m (higher accuracy)" if model.using_640m else "320n (bundled default - lower accuracy)"
    print(f"Model loaded in {time.time() - t0:.2f}s  [using: {model_name}]\n")

    image_paths = sorted(
        p for p in INPUT_DIR.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )

    for path in image_paths:
        t0 = time.time()
        detections = model.analyze(str(path))
        elapsed = time.time() - t0

        if detections:
            labels = ", ".join(f"{d.label}({d.score:.2f})" for d in detections)
            print(f"[TRIGGER] {path.name}: {labels}  ({elapsed*1000:.0f}ms)")
            censor_image(path, detections, OUTPUT_DIR / path.name)
        else:
            print(f"[clear]   {path.name}  ({elapsed*1000:.0f}ms)")

    print(f"\nAnnotated images (if any) written to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
