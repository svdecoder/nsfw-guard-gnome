"""
Not part of the shipped container - a local dev harness to validate
state_machine.py's hysteresis logic (enter/sustain/exit thresholds,
debounce) without needing real screen capture or a real GNOME shim.

Run directly: python3 test_state_machine.py
"""

from dataclasses import dataclass
from state_machine import GuardStateMachine, State


@dataclass
class FakeDetection:
    label: str
    score: float
    box: tuple


class ScriptedModel:
    """Returns a pre-scripted sequence of detection lists, one per call."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def analyze(self, frame):
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[idx]


class FakeCapture:
    def grab_frame(self):
        return "fake-frame"  # analyze() ignores content here


class RecordingOverlay:
    def __init__(self):
        self.events = []

    def show(self, detections):
        self.events.append(("show", [d.label for d in detections]))

    def clear(self):
        self.events.append(("clear",))


def run_n_ticks(sm, n):
    for _ in range(n):
        frame = sm.capture.grab_frame()
        detections = sm.model.analyze(frame)
        if sm.state == State.IDLE:
            sm._tick_idle(detections, frame)
        else:
            sm._tick_active(detections, frame)


HIT = [FakeDetection("FEMALE_GENITALIA_EXPOSED", 0.9, (10, 10, 50, 50))]
WEAK_HIT = [FakeDetection("FEMALE_GENITALIA_EXPOSED", 0.6, (10, 10, 50, 50))]
NONE = []


def test_requires_confirm_frames_to_enter():
    # single hit while idle should NOT trigger (confirm_frames=2)
    model = ScriptedModel([HIT, NONE, NONE, NONE])
    sm = GuardStateMachine(model, FakeCapture(), RecordingOverlay(), confirm_frames=2)
    run_n_ticks(sm, 4)
    assert sm.state == State.IDLE, "single spurious hit should not enter ACTIVE"
    assert sm.overlay.events == [], f"expected no overlay events, got {sm.overlay.events}"
    print("PASS: single spurious hit does not trigger ACTIVE")


def test_enters_on_confirmed_hits():
    model = ScriptedModel([HIT, HIT, NONE, NONE, NONE, NONE])
    sm = GuardStateMachine(
        model, FakeCapture(), RecordingOverlay(),
        confirm_frames=2, exit_debounce_frames=3,
    )
    run_n_ticks(sm, 2)
    assert sm.state == State.ACTIVE, "two consecutive hits should enter ACTIVE"
    assert sm.overlay.events[-1][0] == "show"
    print("PASS: two consecutive hits enters ACTIVE and shows overlay")


def test_exits_after_debounce_not_single_clear_frame():
    # Enter active, then one clear frame should NOT exit (debounce=3)
    model = ScriptedModel([HIT, HIT, NONE])
    sm = GuardStateMachine(
        model, FakeCapture(), RecordingOverlay(),
        confirm_frames=2, exit_debounce_frames=3,
    )
    run_n_ticks(sm, 3)
    assert sm.state == State.ACTIVE, "single clear frame should not exit ACTIVE (debounce)"
    print("PASS: single clear frame during ACTIVE does not exit early")


def test_exits_after_full_debounce():
    model = ScriptedModel([HIT, HIT, NONE, NONE, NONE])
    sm = GuardStateMachine(
        model, FakeCapture(), RecordingOverlay(),
        confirm_frames=2, exit_debounce_frames=3,
    )
    run_n_ticks(sm, 5)
    assert sm.state == State.IDLE, "3 consecutive clear frames should exit ACTIVE"
    assert sm.overlay.events[-1] == ("clear",)
    print("PASS: full debounce clears overlay and returns to IDLE")


def test_weak_hit_below_sustain_threshold_does_not_sustain():
    # sustain_threshold default 0.55; WEAK_HIT score=0.6 is above -> should sustain.
    # Use a truly weak one below threshold to confirm exit path triggers.
    below = [FakeDetection("FEMALE_GENITALIA_EXPOSED", 0.3, (0, 0, 10, 10))]
    model = ScriptedModel([HIT, HIT, below, below, below])
    sm = GuardStateMachine(
        model, FakeCapture(), RecordingOverlay(),
        confirm_frames=2, exit_debounce_frames=3, sustain_threshold=0.55,
    )
    run_n_ticks(sm, 5)
    assert sm.state == State.IDLE, "detections below sustain_threshold should count as clears"
    print("PASS: below-sustain-threshold detections are treated as clears")


def test_holds_last_known_box_during_debounce():
    # This is the actual self-occlusion fix: when a peek comes back
    # with NO detections at all (not just weak ones - e.g. because our
    # own overlay is covering the content), we should redraw the last
    # confidently-known box rather than showing nothing, so the screen
    # doesn't briefly go uncovered while debouncing.
    model = ScriptedModel([HIT, HIT, NONE, NONE])
    sm = GuardStateMachine(
        model, FakeCapture(), RecordingOverlay(),
        confirm_frames=2, exit_debounce_frames=3, sustain_threshold=0.35,
    )
    run_n_ticks(sm, 4)
    assert sm.state == State.ACTIVE, "should still be active, debounce not exhausted yet"
    # the last two "show" events should carry the ORIGINAL hit's box,
    # not an empty list, even though the two most recent ticks found
    # nothing at all
    last_show_events = [e for e in sm.overlay.events if e[0] == "show"]
    assert last_show_events[-1] == ("show", ["FEMALE_GENITALIA_EXPOSED"]), (
        f"expected last-known box to be held during debounce, got {last_show_events[-1]}"
    )
    print("PASS: last known box is held (not cleared) during debounce peeks with zero detections")


if __name__ == "__main__":
    test_requires_confirm_frames_to_enter()
    test_enters_on_confirmed_hits()
    test_exits_after_debounce_not_single_clear_frame()
    test_exits_after_full_debounce()
    test_weak_hit_below_sustain_threshold_does_not_sustain()
    test_holds_last_known_box_during_debounce()
    print("\nAll state machine tests passed.")
