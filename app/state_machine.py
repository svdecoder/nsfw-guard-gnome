"""
IDLE → WARNING → LOGOUT guard loop.

On confirmed NSFW hit:
  1. Send a 'warn' message to the GNOME extension → shows full-screen
     warning overlay + CRITICAL notification with countdown.
  2. Enter WARNING state for 5 seconds, continuously re-checking frames.
  3. If content disappears during the 5-second grace period → dismiss the
     warning, return to IDLE.
  4. If content is still present after 5 seconds → trigger session logout.
"""

import time
import logging
from enum import Enum, auto

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("state_machine")


class GuardState(Enum):
    IDLE = auto()
    WARNING = auto()
    LOGOUT_SENT = auto()


class GuardStateMachine:
    def __init__(
        self,
        model,
        capture,
        overlay_client,
        idle_interval_s: float = 0.3,
        confirm_frames: int = 1,
        warn_grace_s: float = 5.0,
        window_start_hour: int = 22,
        window_end_hour: int = 7,
        window_check_interval_s: float = 30.0,
    ):
        self.model = model
        self.capture = capture
        self.overlay = overlay_client

        self.idle_interval_s = idle_interval_s
        self.confirm_frames = confirm_frames
        self.warn_grace_s = warn_grace_s
        self.window_start_hour = window_start_hour
        self.window_end_hour = window_end_hour
        self.window_check_interval_s = window_check_interval_s

        # IDLE state tracking
        self._pending_confirm = 0

        # WARNING state tracking
        self._warn_started_at = 0.0       # monotonic timestamp when WARNING entered
        self._warn_clear_streak = 0        # consecutive clean frames during WARNING
        self._warn_logout_sent = False     # guard against double-send of logout

        # LOGOUT rate-limiting (redundant now with LOGOUT_SENT state, kept for safety)
        self._logout_sent_at = 0.0

        self.state = GuardState.IDLE

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
            "Starting guard loop (active window %02d:00-%02d:00 local, action=logout, "
            "warn_grace=%.1fs)",
            self.window_start_hour, self.window_end_hour,
            self.warn_grace_s,
        )
        while True:
            if not self._in_active_window():
                time.sleep(self.window_check_interval_s)
                continue

            if self.state == GuardState.IDLE:
                self._tick_idle()
            elif self.state == GuardState.WARNING:
                self._tick_warning()
            else:
                # LOGOUT_SENT — nothing to do, process is likely dying
                # as the session ends. Sleep briefly so we don't busy-loop
                # while waiting for session teardown.
                time.sleep(1.0)

    # ---- IDLE state ----------------------------------------------------------

    def _tick_idle(self):
        """Poll frames, accumulate confirm hits, transition to WARNING on trigger."""
        frame = self.capture.grab_frame()
        if frame is None:
            log.warning("Frame grab failed, retrying")
            time.sleep(1.0)
            return

        detections = self.model.analyze(frame)

        if detections:
            self._pending_confirm += 1
        else:
            self._pending_confirm = 0

        if self._pending_confirm >= self.confirm_frames:
            log.info(
                "TRIGGER (entering WARNING): %s",
                ", ".join(f"{d.label}({d.score:.2f})" for d in detections),
            )
            self._pending_confirm = 0
            self._enter_warning()
            return  # _tick_warning will run on the next loop iteration

        time.sleep(self.idle_interval_s)

    # ---- WARNING state -------------------------------------------------------

    def _enter_warning(self):
        """Send the warning message and transition to WARNING."""
        self._warn_started_at = time.time()
        self._warn_clear_streak = 0
        self.state = GuardState.WARNING
        log.warning("WARNING state entered — %ss grace period started", self.warn_grace_s)
        self.overlay.warn(int(self.warn_grace_s))

    def _tick_warning(self):
        """
        During the WARNING grace period, keep capturing frames.
        If content disappears (clear_streak >= confirm_frames), cancel and
        return to IDLE.
        If the grace period expires, do one final frame check: if content
        is still present, proceed to logout; if it cleared at the last
        moment, cancel instead.
        """
        elapsed = time.time() - self._warn_started_at

        if elapsed >= self.warn_grace_s:
            # Grace expired — one final frame check
            frame = self.capture.grab_frame()
            if frame is None:
                log.warning("Frame grab failed at grace expiry — logging out")
                self._commit_logout()
                return
            detections = self.model.analyze(frame)
            if detections:
                log.info("Content still present after grace period")
                self._commit_logout()
            else:
                log.info("Content cleared right at grace expiry — cancelling")
                self._cancel_warning()
            return

        # Still within grace — capture and recheck
        frame = self.capture.grab_frame()
        if frame is None:
            log.warning("Frame grab failed during WARNING, retrying")
            time.sleep(0.5)
            return

        detections = self.model.analyze(frame)

        if detections:
            self._warn_clear_streak = 0
            time.sleep(self.idle_interval_s)
        else:
            self._warn_clear_streak += 1
            if self._warn_clear_streak >= self.confirm_frames:
                log.info("Content cleared during grace period — cancelling warning")
                self._cancel_warning()
                return
            time.sleep(self.idle_interval_s)

    def _cancel_warning(self):
        """Dismiss the warning overlay and return to IDLE."""
        self.overlay.clear_warn()
        self._warn_clear_streak = 0
        self.state = GuardState.IDLE
        log.info("Returned to IDLE")

    def _commit_logout(self):
        """Grace period expired with content still present — log out."""
        if self._warn_logout_sent:
            return  # already sent, session is tearing down
        self._warn_logout_sent = True
        self.state = GuardState.LOGOUT_SENT
        log.warning("Grace period expired — logging out session")
        self.overlay.logout()