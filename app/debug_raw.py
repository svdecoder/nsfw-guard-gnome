"""
Debug entrypoint. Prints EVERY raw detection (all labels, all scores)
for every image in /data/test_images, with no filtering at all.

Run with:
  docker run --rm --network none \
    -v "$(pwd)/test_images:/data/test_images:ro" \
    -v "$(pwd)/models:/app/models:ro" \
    --entrypoint python nsfw-guard:dev debug_raw.py

Optionally pass one or more filenames (just the basename, e.g.
OIP-2575353038.jpg) to restrict output to those images only, instead
of dumping the whole directory:
  docker run --rm --network none \
    -v "$(pwd)/test_images:/data/test_images:ro" \
    -v "$(pwd)/models:/app/models:ro" \
    --entrypoint python nsfw-guard:dev debug_raw.py \
    OIP-2575353038.jpg OIP-1527857854.jpg

This tells us whether the model is:
  (a) seeing nothing at all on missed images (resolution/preprocessing issue)
  (b) seeing the right region but with a low score (threshold too high)
  (c) seeing it but labeling it as a COVERED class instead of EXPOSED
      (allowlist too strict, or the label itself is legitimately different
      from what we expect)
"""

import sys
from pathlib import Path
from nsfw_model import NsfwModel

INPUT_DIR = Path("/data/test_images")


def main():
    model = NsfwModel(high_conf_threshold=0.45)
    model_name = "640m (higher accuracy)" if model.using_640m else "320n (bundled default - lower accuracy)"
    print(f"[using: {model_name}]")

    image_paths = sorted(
        p for p in INPUT_DIR.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )

    filter_names = set(sys.argv[1:])
    if filter_names:
        missing = filter_names - {p.name for p in image_paths}
        if missing:
            print(f"  [!] not found in {INPUT_DIR}: {', '.join(sorted(missing))}")
        image_paths = [p for p in image_paths if p.name in filter_names]

    if not image_paths:
        print(f"No matching images found in {INPUT_DIR}")
        return

    for path in image_paths:
        raw = model.analyze_raw(str(path))
        print(f"\n=== {path.name} ===")
        if not raw:
            print("  (no detections at all - model saw nothing)")
            continue
        # sort by score descending so the most confident hits are on top
        for item in sorted(raw, key=lambda d: -d["score"]):
            print(f"  {item['class']:30s} score={item['score']:.3f}  box={item['box']}")


if __name__ == "__main__":
    main()
