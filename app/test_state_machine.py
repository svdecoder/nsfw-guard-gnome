#!/usr/bin/env python3
"""
Unit tests for the IDLE -> WARNING -> LOGOUT state machine and overlay client.

Run from the repo root or the app/ directory:

    python3 app/test_state_machine.py
"""

import os
import sys
import time as _time

# Allow running from anywhere: import modules from the same directory as
# this file, regardless of the current working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from state_machine import GuardStateMachine, GuardState  # noqa: E402
from overlay_client import OverlayClient  # noqa: E402


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeDetection:
    def __init__(self, label="FEMALE_GENITALIA_EXPOSED", score=0.9, box=(10, 10, 50, 50)):
        self.label = label
        self.score = score
        self.box = box

    def __repr__(self):
        return f"FakeDetection({self.label}, {self.score:.2f})"


HIT = [
    FakeDetection("FEMALE_GENITALIA_EXPOSED", 0.95),
    FakeDetection("ANUS_EXPOSED", 0.88),
]
NONE = []


class ScriptedModel:
    """Returns a pre-scripted sequence of detection lists, one per analyze() call."""

    def __init__(self, script):
        self.script = list(script)
        self.pos = 0

    def analyze(self, frame):
        idx = min(self.pos, len(self.script) - 1)
        self.pos += 1
        return self.script[idx]


class FakeCapture:
    def grab_frame(self):
        return b"fake-frame"


class RecordingOverlay:
    def __init__(self):
        self.events = []

    def warn(self, seconds):
        self.events.append(("warn", seconds))

    def clear_warn(self):
        self.events.append(("clear_warn",))

    def logout(self):
        self.events.append(("logout",))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_sm(model, overlay, **kwargs):
    kwargs.setdefault("confirm_frames", 1)
    kwargs.setdefault("warn_grace_s", 5.0)
    kwargs.setdefault("window_start_hour", 0)
    kwargs.setdefault("window_end_hour", 0)
    return GuardStateMachine(model, FakeCapture(), overlay, **kwargs)


def in_warning(sm, expired=False):
    """Force the machine into WARNING.

    expired=True  -> grace period already elapsed (tests the expiry path).
    expired=False -> fresh warning, still well inside the grace period.
    """
    sm.state = GuardState.WARNING
    sm._warn_started_at = 0.0 if expired else _time.time()


# ---------------------------------------------------------------------------
# IDLE state
# ---------------------------------------------------------------------------

def test_idle_hit_enters_warning():
    overlay = RecordingOverlay()
    sm = make_sm(ScriptedModel([HIT, NONE]), overlay)
    sm._tick_idle()
    assert sm.state == GuardState.WARNING, f"expected WARNING, got {sm.state}"
    assert overlay.events == [("warn", 5)], f"expected warn(5), got {overlay.events}"
    print("PASS: confirmed hit enters WARNING and sends warn(5)")


def test_idle_no_hit_stays_idle():
    overlay = RecordingOverlay()
    sm = make_sm(ScriptedModel([NONE, NONE, NONE]), overlay)
    for _ in range(3):
        sm._tick_idle()
    assert sm.state == GuardState.IDLE
    assert overlay.events == [], f"expected no events, got {overlay.events}"
    print("PASS: clean frames keep the machine in IDLE")


def test_idle_confirm_frames_requires_two_hits():
    overlay = RecordingOverlay()
    sm = make_sm(ScriptedModel([HIT, NONE]), overlay, confirm_frames=2)
    sm._tick_idle()  # hit -> pending=1
    assert sm.state == GuardState.IDLE
    sm._tick_idle()  # clean -> pending=0, no trigger
    assert sm.state == GuardState.IDLE
    assert overlay.events == [], "single hit with confirm_frames=2 must not trigger"
    print("PASS: confirm_frames=2 requires two consecutive hits")


# ---------------------------------------------------------------------------
# WARNING -> LOGOUT
# ---------------------------------------------------------------------------

def test_warning_grace_expiry_logs_out():
    overlay = RecordingOverlay()
    sm = make_sm(ScriptedModel([HIT]), overlay, warn_grace_s=0.0)
    in_warning(sm, expired=True)
    sm._tick_warning()  # grace expired -> final frame still hitting -> logout
    assert sm.state == GuardState.LOGOUT_SENT, f"got {sm.state}"
    assert ("logout",) in overlay.events
    print("PASS: grace expiry with content still present logs out")


def test_warning_cancels_when_content_cleared():
    overlay = RecordingOverlay()
    sm = make_sm(ScriptedModel([NONE, NONE]), overlay, confirm_frames=1, warn_grace_s=3600.0)
    in_warning(sm, expired=False)
    sm._tick_warning()  # clean during grace -> clear_streak=1 -> cancel immediately
    assert sm.state == GuardState.IDLE, f"got {sm.state}"
    assert ("clear_warn",) in overlay.events
    assert ("logout",) not in overlay.events
    print("PASS: content cleared during grace period cancels the warning")


def test_warning_clear_streak_resets_on_reappearance():
    overlay = RecordingOverlay()
    # sequence: clean, hit, clean, clean  (confirm_frames=2 -> need 2 clears to cancel)
    sm = make_sm(ScriptedModel([NONE, HIT, NONE, NONE]), overlay,
                 confirm_frames=2, warn_grace_s=3600.0)
    in_warning(sm, expired=False)

    sm._tick_warning()          # clean -> streak=1
    assert sm.state == GuardState.WARNING
    assert sm._warn_clear_streak == 1

    sm._tick_warning()          # hit -> streak resets to 0
    assert sm._warn_clear_streak == 0

    sm._tick_warning()          # clean -> streak=1
    assert sm._warn_clear_streak == 1

    sm._tick_warning()          # clean -> streak=2 == confirm_frames -> cancel
    assert sm.state == GuardState.IDLE
    assert ("clear_warn",) in overlay.events
    print("PASS: clear streak resets when content reappears, cancels after 2 clears")


def test_warning_grace_expiry_cancels_if_cleared_at_last_moment():
    overlay = RecordingOverlay()
    sm = make_sm(ScriptedModel([NONE]), overlay, warn_grace_s=0.0)
    in_warning(sm, expired=True)
    sm._tick_warning()  # grace expired, but final frame is clean -> cancel instead of logout
    assert sm.state == GuardState.IDLE, f"got {sm.state}"
    assert ("logout",) not in overlay.events
    print("PASS: content gone at the instant of expiry cancels instead of logging out")


# ---------------------------------------------------------------------------
# Active window scheduling
# ---------------------------------------------------------------------------

def test_active_window_24_7():
    sm = make_sm(ScriptedModel([]), RecordingOverlay(),
                 window_start_hour=0, window_end_hour=0)
    assert sm._in_active_window() is True
    print("PASS: start == end means always active")


def test_active_window_normal():
    sm = make_sm(ScriptedModel([]), RecordingOverlay(),
                 window_start_hour=8, window_end_hour=17)
    orig = _time.localtime
    try:
        _time.localtime = lambda: type("T", (), {"tm_hour": 12})
        assert sm._in_active_window() is True
        _time.localtime = lambda: type("T", (), {"tm_hour": 7})
        assert sm._in_active_window() is False
        _time.localtime = lambda: type("T", (), {"tm_hour": 17})
        assert sm._in_active_window() is False  # end hour is exclusive
    finally:
        _time.localtime = orig
    print("PASS: normal window [8, 17)")


def test_active_window_overnight():
    sm = make_sm(ScriptedModel([]), RecordingOverlay(),
                 window_start_hour=22, window_end_hour=7)
    orig = _time.localtime
    try:
        _time.localtime = lambda: type("T", (), {"tm_hour": 23})
        assert sm._in_active_window() is True
        _time.localtime = lambda: type("T", (), {"tm_hour": 3})
        assert sm._in_active_window() is True
        _time.localtime = lambda: type("T", (), {"tm_hour": 8})
        assert sm._in_active_window() is False
    finally:
        _time.localtime = orig
    print("PASS: overnight window [22, 24) | [0, 7)")


# ---------------------------------------------------------------------------
# Overlay client wire format
# ---------------------------------------------------------------------------

class RecordingSocketClient(OverlayClient):
    def __init__(self):
        super().__init__("/tmp/fake-nsfw-guard.sock")
        self.sent = []

    def _send(self, payload):
        self.sent.append(payload)


def test_overlay_client_wire_format():
    client = RecordingSocketClient()
    client.warn(5)
    client.clear_warn()
    client.logout()
    assert client.sent == [
        {"action": "warn", "seconds": 5},
        {"action": "clear_warn"},
        {"action": "logout"},
    ], f"unexpected wire messages: {client.sent}"
    print("PASS: wire format warn/clear_warn/logout")


# ---------------------------------------------------------------------------

def main():
    test_idle_hit_enters_warning()
    test_idle_no_hit_stays_idle()
    test_idle_confirm_frames_requires_two_hits()
    test_warning_grace_expiry_logs_out()
    test_warning_cancels_when_content_cleared()
    test_warning_clear_streak_resets_on_reappearance()
    test_warning_grace_expiry_cancels_if_cleared_at_last_moment()
    test_active_window_24_7()
    test_active_window_normal()
    test_active_window_overnight()
    test_overlay_client_wire_format()
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()