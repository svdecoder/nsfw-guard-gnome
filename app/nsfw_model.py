"""
Thin wrapper around NudeNet's NudeDetector.

Design goal: near-zero false positives. We do this by:
  1. Only reacting to a small allowlist of "unambiguous" explicit classes,
     ignoring borderline/covered/ambiguous classes entirely.
  2. Requiring a high confidence threshold for those classes.
  3. Exposing a lower "sustain" threshold used only once we're already
     in ACTIVE state (hysteresis - implemented in the state machine, not here).
"""

import os
import logging
from dataclasses import dataclass
import numpy as np
import onnxruntime
import nudenet as _nudenet_pkg
from nudenet import NudeDetector

log = logging.getLogger("nsfw_model")

# NudeNet v3 label set (base model). We deliberately only trigger on
# classes that are unambiguous. Covered / soft / borderline classes
# are intentionally excluded to keep false positives near zero.
TRIGGER_LABELS = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
}

# Everything else NudeNet can detect (for reference / future tuning):
# FEMALE_BREAST_COVERED, FEMALE_GENITALIA_COVERED, BUTTOCKS_COVERED,
# BELLY_EXPOSED, ARMPITS_EXPOSED, ARMPITS_COVERED, FACE_FEMALE,
# FACE_MALE, MALE_BREAST_EXPOSED, FEET_EXPOSED, FEET_COVERED,
# BELLY_COVERED, MALE_GENITALIA_EXPOSED (see above)


@dataclass
class Detection:
    label: str
    score: float
    box: tuple  # (x, y, w, h) in pixel coords of the input image


class NsfwModel:
    def __init__(
        self,
        high_conf_threshold: float = 0.45,
        model_path: str = "/app/models/640m.onnx",
        use_gpu: bool = True,
    ):
        """
        high_conf_threshold: minimum confidence for a TRIGGER_LABELS hit
                              to count at all. This is the ENTER threshold.
                              The state machine applies a separate, lower
                              SUSTAIN threshold once already active.

        NOTE on this number: 0.75 (the original guess) was far too
        conservative against real images - genuine EXPOSED-class hits
        on actual explicit content commonly score in the 0.4-0.7 range
        with this model (see debug_raw.py output from real test runs).
        0.45 is a starting point based on that data, not a guarantee -
        keep validating against your own test set as you go.

        model_path: path to the larger, more accurate 640m model (640x640,
                    yolov8m-based). This catches smaller/harder regions
                    (e.g. ANUS_EXPOSED, distant subjects) that the smaller
                    default 320n model misses entirely - see the
                    false-negative debugging that led to this change.
                    Must be downloaded manually and bind-mounted in at
                    runtime (see README.md "Getting the 640m model") -
                    GitHub gates this specific release asset behind a
                    login redirect, so it can't be fetched automatically
                    at build time. Falls back to the bundled 320n model
                    if this path doesn't exist, so the container still
                    works out of the box, just less accurately.

        use_gpu: try to run inference on the GPU (CUDA) if available.
                 IMPORTANT CAVEAT: NudeDetector's own constructor accepts
                 a `providers` argument but has a bug where it never
                 actually forwards it to onnxruntime.InferenceSession
                 (see the commented-out line in nudenet's source) - so
                 we can't just pass providers=[...] to NudeDetector and
                 expect it to work. Instead we let NudeDetector build its
                 session normally (CPU), then swap in our own
                 InferenceSession built with CUDA providers, falling
                 back silently to the CPU session if CUDA isn't actually
                 usable at runtime (get_available_providers() only
                 reflects what onnxruntime was COMPILED with, not
                 whether the CUDA/cuDNN shared libraries are actually
                 loadable on this machine - that can only be discovered
                 by actually trying).
        """
        if model_path and os.path.exists(model_path):
            resolved_path = model_path
            resolution = 640
            self.using_640m = True
        else:
            resolved_path = os.path.join(os.path.dirname(_nudenet_pkg.__file__), "320n.onnx")
            resolution = 320
            self.using_640m = False

        self.detector = NudeDetector(model_path=resolved_path, inference_resolution=resolution)
        self.high_conf_threshold = high_conf_threshold

        self.gpu_active = False
        if use_gpu:
            self.gpu_active = self._try_enable_gpu(resolved_path)

    def _try_enable_gpu(self, model_path: str) -> bool:
        try:
            available = onnxruntime.get_available_providers()
        except Exception as e:
            log.warning("Could not query onnxruntime providers: %s", e)
            return False

        if "CUDAExecutionProvider" not in available:
            log.info(
                "CUDAExecutionProvider not compiled into this onnxruntime build "
                "(available: %s) - staying on CPU",
                available,
            )
            return False

        try:
            gpu_session = onnxruntime.InferenceSession(
                model_path,
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            )
        except Exception as e:
            # This is the common failure mode: CUDAExecutionProvider is
            # compiled in, but the actual CUDA/cuDNN .so files aren't
            # loadable at runtime (missing/mismatched versions, no
            # --gpus flag on `docker run`, etc). Fail soft - CPU still
            # works, just slower.
            log.warning(
                "CUDA execution provider is available but failed to initialize "
                "(%s) - falling back to CPU. Did you run with `--gpus all`? "
                "Is the CUDA/cuDNN version installed compatible with this "
                "onnxruntime-gpu build?",
                e,
            )
            return False

        active_providers = gpu_session.get_providers()
        if not active_providers or active_providers[0] != "CUDAExecutionProvider":
            log.warning(
                "Requested CUDA but onnxruntime silently picked providers=%s instead "
                "- falling back to CPU",
                active_providers,
            )
            return False

        self.detector.onnx_session = gpu_session
        log.info("GPU (CUDA) inference enabled")
        return True

    def analyze(self, image_path_or_array) -> list[Detection]:
        """
        Accepts either a file path (str) or an (H, W, 3) RGB numpy array
        (as produced by capture.ScreenCapture.grab_frame).

        IMPORTANT: NudeNet's underlying detector reads file paths via
        cv2.imread (BGR) and, when given a numpy array directly, uses
        it as-is with NO color conversion - it silently assumes BGR.
        Our capture pipeline produces RGB frames, so we must flip
        channels here or every detection will be run on color-swapped
        input, which will hurt both accuracy and false-positive rate.
        """
        if isinstance(image_path_or_array, np.ndarray):
            image_path_or_array = image_path_or_array[:, :, ::-1]  # RGB -> BGR

        raw = self.detector.detect(image_path_or_array)
        results = []
        for item in raw:
            label = item["class"]
            score = float(item["score"])
            if label not in TRIGGER_LABELS:
                continue
            if score < self.high_conf_threshold:
                continue
            x, y, w, h = item["box"]
            results.append(Detection(label=label, score=score, box=(x, y, w, h)))
        return results

    def analyze_raw(self, image_path_or_array) -> list[dict]:
        """
        Debug helper: returns EVERY raw detection from the model,
        completely unfiltered - no label allowlist, no threshold.
        Use this to diagnose false negatives (is the model seeing
        anything at all? what labels/scores is it actually producing?).
        """
        if isinstance(image_path_or_array, np.ndarray):
            image_path_or_array = image_path_or_array[:, :, ::-1]
        return self.detector.detect(image_path_or_array)
