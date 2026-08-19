"""
IDLE / ACTIVE state machine.

IDLE:   poll once every IDLE_INTERVAL_S seconds. On a high-confidence
        trigger hit, switch to ACTIVE immediately.

ACTIVE: the overlay stays drawn, held in its last known position,
        without re-capturing every tick. Periodically (every
        PEEK_INTERVAL_S seconds) we briefly hide the overlay, capture
        one real frame, and decide whether to redraw (updated
        position) or exit.

        Why "peek" instead of capturing every tick while active: screen
        capture via xdg-desktop-portal/PipeWire captures the final
        COMPOSITED screen, which includes our own overlay (it's a Shell
        UI actor, drawn by the compositor like everything else). If we
        captured every tick, frame N+1 would see frame N's own black
        box instead of the real content underneath it, immediately
        read as "content is gone", and exit active within a few ticks
        even though the real content never moved - a self-defeating
        feedback loop. Peeking (hide -> capture -> redraw) breaks that
        loop at the cost of a brief, real exposure window on each peek.

This hysteresis (enter high, sustain lower, exit needs consecutive
clears) is what keeps a single noisy frame from causing flicker.
"""

import time
import logging
import os
from enum import Enum, auto
from nsfw_model import Detection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("state_machine")


class State(Enum):
    IDLE = auto()
    ACTIVE = auto()


class GuardStateMachine:
    def __init__(
        self,
        model,
        capture,
        overlay_client,
        idle_interval_s: float = 0.3,
        active_interval_s: float = 0.3,
        peek_interval_s: float = 1.0,
        peek_settle_s: float = 0.02,
        wide_check_interval_s: float = 10.0,
        alert_peek_interval_s: float = 0.3,
        sustain_threshold: float = 0.35,
        exit_debounce_frames: int = 2,
        confirm_frames: int = 1,
        debug_snapshot_dir: str = None,
        action_mode: str = "logout",
        window_start_hour: int = 22,
        window_end_hour: int = 7,
        window_check_interval_s: float = 30.0,
    ):
        self.model = model
        self.capture = capture
        self.overlay = overlay_client

        self.idle_interval_s = idle_interval_s
        self.active_interval_s = active_interval_s
        self.peek_interval_s = peek_interval_s
        self.peek_settle_s = peek_settle_s
        # wide_check_interval_s (hybrid mode only): how often we pull a
        # frame from the MONITOR stream to check for nsfw content
        # anywhere on screen outside the primary window. This is the
        # closest approximation of "check other windows" achievable
        # without a per-window consent dialog (see capture.py docstring).
        self.wide_check_interval_s = wide_check_interval_s
        # alert_peek_interval_s (hybrid mode only): once a wide-scan hit
        # is found, how often we peek-blink the monitor stream to keep
        # tracking it - faster than the default peek_interval_s, per the
        # "faster/shorter blinking" ask, since this only runs while
        # something's already been confirmed nearby, not all the time.
        self.alert_peek_interval_s = alert_peek_interval_s
        self.sustain_threshold = sustain_threshold
        self.exit_debounce_frames = exit_debounce_frames
        self.confirm_frames = confirm_frames
        self.debug_snapshot_dir = debug_snapshot_dir

        # action_mode: "logout" (default) triggers a real session logout
        # via the GNOME extension the moment a hit is confirmed - no
        # ongoing tracking/box needed, since the session is ending.
        # "box" keeps the original black-box overlay behavior instead.
        self.action_mode = action_mode
        self.window_start_hour = window_start_hour
        self.window_end_hour = window_end_hour
        self.window_check_interval_s = window_check_interval_s
        self._logout_sent_at = 0.0

        if self.debug_snapshot_dir:
            os.makedirs(self.debug_snapshot_dir, exist_ok=True)
            log.info("Debug snapshots enabled -> %s", self.debug_snapshot_dir)

        self.state = State.IDLE
        self._clear_streak = 0
        self._pending_confirm = 0  # frames in a row with a hit, while still IDLE
        self._last_peek_time = 0.0
        self._last_known_detections = []  # most recent sustaining hit, held during debounce

        # hybrid-mode-only tracking (unused otherwise)
        self._window_detections = []
        self._wide_detections = []
        self._wide_active = False
        self._wide_clear_streak = 0
        self._last_wide_check_time = 0.0
        self._last_wide_peek_time = 0.0

    def _in_active_window(self):
        """
        True if the current local clock hour falls within
        [window_start_hour, window_end_hour). Handles overnight windows
        that wrap past midnight (e.g. 22 -> 7) as well as normal ones.
        start == end means "always active" (24/7).
        """
        start, end = self.window_start_hour, self.window_end_hour
        if start == end:
            return True
        hour = time.localtime().tm_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def run_forever(self):
        log.info(
            "Starting guard loop in IDLE state (active window %02d:00-%02d:00 local, action=%s)",
            self.window_start_hour, self.window_end_hour, self.action_mode,
        )
        while True:
            if not self._in_active_window():
                # Outside the configured hours: don't capture, don't
                # run inference, just wait and recheck the clock. Keeps
                # this at effectively zero CPU/GPU use off-hours.
                time.sleep(self.window_check_interval_s)
                continue

            if self.capture.capture_mode == "hybrid":
                self._run_hybrid_tick()
            elif self.state == State.IDLE:
                self._run_idle_tick()
            else:
                self._run_active_tick()

    def _capture_and_detect(self):
        """Shared capture+inference+timing, returns (frame, detections) or (None, None)."""
        loop_t0 = time.time()

        capture_t0 = time.time()
        frame = self.capture.grab_frame()
        capture_ms = (time.time() - capture_t0) * 1000

        if frame is None:
            log.warning("Frame grab failed, retrying")
            time.sleep(1.0)
            return None, None

        infer_t0 = time.time()
        detections = self.model.analyze(frame)
        infer_ms = (time.time() - infer_t0) * 1000

        total_ms = (time.time() - loop_t0) * 1000
        log.info(
            "[%s] capture=%.0fms infer=%.0fms total=%.0fms",
            self.state.name, capture_ms, infer_ms, total_ms,
        )
        return frame, detections

    def _run_hybrid_tick(self):
        now = time.time()

        # 1. Fast primary defense: the window stream, every tick, no
        #    blink (window capture never contains our own overlay).
        window_frame, window_offset = self.capture.grab_frame_role("window")
        if window_frame is not None:
            raw = self.model.analyze(window_frame)
            self._window_detections = self._shift_detections(raw, window_offset)
            if raw:
                self._save_debug_snapshot(window_frame, raw, label="window")

        # 2. Slow wide scan: pull the monitor stream every
        #    wide_check_interval_s to catch anything outside the window.
        #    If a wide alert is currently active, peek-blink it faster
        #    (alert_peek_interval_s) instead, purely to keep the wide
        #    box's own tracking reasonably tight while it's up - the
        #    decision to CLEAR a wide alert still only re-checks on the
        #    slow wide_check_interval_s cadence (see docstring above).
        check_interval = self.alert_peek_interval_s if self._wide_active else self.wide_check_interval_s
        if now - self._last_wide_check_time >= check_interval:
            self._last_wide_check_time = now
            self._do_wide_check()

        self._publish_hybrid_overlay()
        time.sleep(self.active_interval_s)

    def _do_wide_check(self):
        # Hide any currently-shown wide box before sampling the monitor,
        # same self-occlusion reasoning as monitor-mode peeking - the
        # window box is untouched (it's never visible in this stream).
        if self._wide_active:
            self.overlay.show(self._window_detections)  # re-show window-only, drop wide box
            time.sleep(self.peek_settle_s)

        monitor_frame, _ = self.capture.grab_frame_role("monitor")
        if monitor_frame is None:
            log.warning("Wide check: monitor frame grab failed")
            return

        detections = self.model.analyze(monitor_frame)
        sustaining = [d for d in detections if d.score >= self.sustain_threshold]

        if sustaining:
            if not self._wide_active:
                log.info(
                    "WIDE ALERT: %s",
                    ", ".join(f"{d.label}({d.score:.2f})" for d in sustaining),
                )
                self._save_debug_snapshot(monitor_frame, sustaining, label="wide")
            self._wide_active = True
            self._wide_clear_streak = 0
            self._wide_detections = sustaining
        else:
            self._wide_clear_streak += 1
            if self._wide_active and self._wide_clear_streak >= self.exit_debounce_frames:
                log.info("WIDE ALERT cleared")
                self._wide_active = False
                self._wide_detections = []
                self._wide_clear_streak = 0

    def _publish_hybrid_overlay(self):
        combined = self._window_detections + (self._wide_detections if self._wide_active else [])
        if combined:
            if self.action_mode == "logout":
                self._trigger_logout()
            else:
                self.overlay.show(combined)
        elif self.action_mode != "logout":
            self.overlay.clear()

    def _trigger_logout(self):
        # Rate-limit so a run of hits (e.g. every hybrid tick, or the
        # next idle-loop confirm right after this one) doesn't spam
        # logout requests - one every couple seconds is plenty since
        # gnome-session-quit isn't instant.
        now = time.time()
        if now - self._logout_sent_at < 2.0:
            return
        self._logout_sent_at = now
        log.warning("NSFW content confirmed during active window -> logging out session")
        self.overlay.logout()

    @staticmethod
    def _shift_detections(detections, offset):
        ox, oy = offset
        if ox == 0 and oy == 0:
            return detections
        return [
            Detection(label=d.label, score=d.score, box=(d.box[0] + ox, d.box[1] + oy, d.box[2], d.box[3]))
            for d in detections
        ]

    def _run_idle_tick(self):
        frame, detections = self._capture_and_detect()
        if frame is not None:
            self._tick_idle(detections, frame)
        time.sleep(self.idle_interval_s)

    def _run_active_tick(self):
        # window-mode capture never sees our own overlay (the captured
        # window surface is composited before Shell-level UI chrome, see
        # capture.py's capture_mode docstring), so there's no self-
        # occlusion problem to work around - just capture and detect on
        # every tick, no hide/show dance, no blink at all.
        if getattr(self.capture, "capture_mode", "monitor") == "window":
            frame, detections = self._capture_and_detect()
            if frame is not None:
                self._tick_active(detections, frame)
            time.sleep(self.active_interval_s)
            return

        now = time.time()
        if now - self._last_peek_time < self.peek_interval_s:
            # Hold: box stays exactly as it is, no capture this tick -
            # avoids re-capturing our own overlay every 0.3s.
            time.sleep(self.active_interval_s)
            return

        # Peek: hide the box, let the compositor actually redraw
        # without it, then sample ONE frame - then immediately re-cover
        # with the last known box BEFORE running inference. Inference
        # (~150-350ms on this hardware) is far slower than capture
        # (~2-4ms), and there's no need for the overlay to stay hidden
        # while we think about the frame we already captured - only
        # the capture itself needs the box out of the way. This keeps
        # the real exposure window down to roughly capture time +
        # peek_settle_s instead of capture + inference time, which is
        # the difference between a near-imperceptible blink and a
        # visible one.
        self.overlay.clear()
        time.sleep(self.peek_settle_s)

        capture_t0 = time.time()
        frame = self.capture.grab_frame()
        capture_ms = (time.time() - capture_t0) * 1000

        # Re-cover immediately, before inference - this is the whole
        # point of splitting capture from inference here.
        self.overlay.show(self._last_known_detections)

        self._last_peek_time = now

        if frame is None:
            log.warning("Frame grab failed during peek, will retry next peek")
            return

        infer_t0 = time.time()
        detections = self.model.analyze(frame)
        infer_ms = (time.time() - infer_t0) * 1000

        log.info(
            "[%s] capture=%.0fms infer=%.0fms (overlay re-shown before infer)",
            self.state.name, capture_ms, infer_ms,
        )

        self._tick_active(detections, frame)

    def _tick_idle(self, detections, frame):
        # Require confirm_frames consecutive high-confidence hits before
        # switching modes, to filter one-off spurious detections even at
        # the entry threshold (extra safety margin beyond the model's
        # own high_conf_threshold).
        if detections:
            self._pending_confirm += 1
        else:
            self._pending_confirm = 0

        if self._pending_confirm >= self.confirm_frames:
            log.info(
                "TRIGGER: %s",
                ", ".join(f"{d.label}({d.score:.2f})" for d in detections),
            )
            self._save_debug_snapshot(frame, detections)
            self._pending_confirm = 0

            if self.action_mode == "logout":
                self._trigger_logout()
                # Stay in IDLE - the session is ending, there's nothing
                # left to track. If logout is somehow slow/blocked, the
                # next confirmed hit will just re-send it.
                return

            self.state = State.ACTIVE
            self._clear_streak = 0
            self._last_peek_time = time.time()  # this frame counts as the first peek
            self._last_known_detections = detections
            self.overlay.show(detections)

    def _tick_active(self, detections, frame):
        sustaining = [d for d in detections if d.score >= self.sustain_threshold]

        if sustaining:
            self._clear_streak = 0
            self._last_known_detections = sustaining
            self.overlay.show(sustaining)
        else:
            self._clear_streak += 1
            if self._clear_streak >= self.exit_debounce_frames:
                log.info("EXIT active -> idle")
                self.overlay.clear()
                self.state = State.IDLE
                self._clear_streak = 0
                self._last_known_detections = []
            else:
                # Not exiting yet - hold the box at its last confirmed
                # position while we debounce, rather than leaving the
                # screen uncovered (this peek itself found nothing, but
                # we don't yet trust that - could be a momentary miss).
                log.info(
                    "PEEK found no sustaining hit (%d/%d debounce) - holding last known box",
                    self._clear_streak, self.exit_debounce_frames,
                )
                self.overlay.show(self._last_known_detections)

    def _save_debug_snapshot(self, frame, detections, label="active"):
        """
        Saves the actual captured frame with the model's detection boxes
        drawn on it, for visual verification that box coordinates line up
        with the real on-screen content. Only runs when
        debug_snapshot_dir was passed in (opt-in - this is not free: an
        extra cv2 import + disk write per trigger).
        """
        if not self.debug_snapshot_dir:
            return
        try:
            import cv2
            # frame is RGB (from capture.py); cv2 wants BGR for correct
            # on-disk colors when opened with any standard image viewer.
            img = frame[:, :, ::-1].copy()
            for d in detections:
                x, y, w, h = [int(v) for v in d.box]
                cv2.rectangle(img, (x, y), (x + w, y + h), (0, 0, 255), 3)
                cv2.putText(
                    img, f"{d.label} {d.score:.2f}", (x, max(0, y - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2,
                )
            path = os.path.join(self.debug_snapshot_dir, f"snapshot_{label}_{int(time.time())}.png")
            cv2.imwrite(path, img)
            log.info("Saved debug snapshot: %s", path)
        except Exception as e:
            log.warning("Failed to save debug snapshot: %s", e)
