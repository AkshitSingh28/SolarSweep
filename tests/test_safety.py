"""Interlocks.

Each test here corresponds to a way v1 would have damaged itself.
"""

from __future__ import annotations

import time

import pytest

from solarsweep.hal import InputId, MotorId
from solarsweep.hal.base import Clock
from solarsweep.safety import (
    Deadline,
    EStopLatch,
    SafetyAbort,
    SafetyMonitor,
    StallDetector,
    StopReason,
    Watchdog,
)


class FakeClock(Clock):
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


# -- E-stop latch ----------------------------------------------------------


def test_latch_persists_until_explicitly_reset():
    latch = EStopLatch()
    assert not latch.tripped
    latch.trip(StopReason.OBSTACLE, "something on the rail")
    assert latch.tripped
    # No amount of asking nicely clears it.
    with pytest.raises(SafetyAbort):
        latch.raise_if_tripped()
    assert latch.tripped
    latch.reset()
    assert not latch.tripped


def test_latch_keeps_the_first_reason():
    """The first thing that went wrong is the useful one; later trips are
    usually consequences of it."""
    latch = EStopLatch()
    latch.trip(StopReason.STALL, "carriage jammed")
    latch.trip(StopReason.TIMEOUT, "and then this timed out")
    assert latch.reason is StopReason.STALL
    assert "jammed" in latch.snapshot()["detail"]


# -- Deadlines -------------------------------------------------------------


def test_deadline_raises_with_the_purpose_in_the_message():
    clock = FakeClock()
    deadline = Deadline(clock, 10.0, "homing to the near end stop")
    deadline.check()
    clock.t = 10.1
    with pytest.raises(SafetyAbort, match="homing to the near end stop") as exc:
        deadline.check()
    assert exc.value.reason is StopReason.TIMEOUT


def test_deadline_extension_returns_only_paused_time():
    clock = FakeClock()
    deadline = Deadline(clock, 10.0, "traverse")
    clock.t = 8.0
    deadline.extend(5.0)  # as if paused for 5s
    assert deadline.elapsed == pytest.approx(3.0)
    clock.t = 18.1
    with pytest.raises(SafetyAbort):
        deadline.check()


# -- Stall detection -------------------------------------------------------


def test_stall_fires_when_commanded_motion_produces_none():
    clock = FakeClock()
    detector = StallDetector(clock, window_s=2.0, min_travel_mm=4.0)
    detector.arm(100.0)
    clock.t = 1.0
    detector.update(100.5)  # barely moved
    clock.t = 2.5
    with pytest.raises(SafetyAbort, match="check the rail") as exc:
        detector.update(100.5)
    assert exc.value.reason is StopReason.STALL


def test_stall_does_not_fire_while_progress_continues():
    clock = FakeClock()
    detector = StallDetector(clock, window_s=2.0, min_travel_mm=4.0)
    detector.arm(0.0)
    for step in range(1, 20):
        clock.t = step * 1.0
        detector.update(step * 10.0)  # 10mm per second


def test_disarmed_detector_ignores_everything():
    clock = FakeClock()
    detector = StallDetector(clock, window_s=1.0, min_travel_mm=4.0)
    detector.arm(0.0)
    detector.disarm()
    clock.t = 100.0
    detector.update(0.0)  # no exception


# -- Watchdog --------------------------------------------------------------


def test_watchdog_cuts_the_outputs_when_the_loop_goes_quiet(board):
    """Measured in wall-clock time on purpose: a wedged process is a
    wall-clock event, so this test really does wait."""
    latch = EStopLatch()
    board.set_motor(MotorId.DRIVE, 60)
    dog = Watchdog(board, latch, timeout_s=0.25)
    dog.start()
    try:
        deadline = time.monotonic() + 5.0
        while not latch.tripped and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        dog.stop()
    assert latch.tripped
    assert latch.reason is StopReason.WATCHDOG
    assert board.state.drive_duty == 0


def test_watchdog_stays_quiet_while_petted(board):
    latch = EStopLatch()
    dog = Watchdog(board, latch, timeout_s=0.5)
    dog.start()
    try:
        for _ in range(10):
            dog.pet()
            time.sleep(0.05)
    finally:
        dog.stop()
    assert not latch.tripped


# -- Monitor ---------------------------------------------------------------


def test_preflight_refuses_to_start_in_the_rain(settings, board):
    monitor = SafetyMonitor(board, settings, EStopLatch())
    board.state.raining = True
    with pytest.raises(SafetyAbort, match="rain") as exc:
        monitor.preflight()
    assert exc.value.reason is StopReason.RAIN


def test_preflight_refuses_with_the_estop_held(settings, board):
    latch = EStopLatch()
    monitor = SafetyMonitor(board, settings, latch)
    board.press_estop()
    with pytest.raises(SafetyAbort):
        monitor.preflight()
    assert latch.tripped  # and it stays tripped after the button is released
    board.release_estop()
    assert latch.tripped


def test_tick_raises_rather_than_returning_a_status(settings, board):
    """The core correction to v1: an interlock cannot be ignored by a caller
    who forgets to check a return value."""
    monitor = SafetyMonitor(board, settings, EStopLatch())
    monitor.tick()
    board.state.obstacle_present = True
    with pytest.raises(SafetyAbort) as exc:
        monitor.tick()
    assert exc.value.reason is StopReason.OBSTACLE


def test_overcurrent_needs_to_persist_past_the_grace_window(settings):
    from dataclasses import replace

    from solarsweep.hal.sim import SimBoard

    settings = replace(settings, safety=replace(settings.safety, overcurrent_a=1.0,
                                                overcurrent_grace_s=1.0))
    # The board has to share these settings — it is what decides whether a
    # current sensor exists at all.
    board = SimBoard(settings)
    monitor = SafetyMonitor(board, settings, EStopLatch())
    board.set_motor(MotorId.DRIVE, 100)
    board.state.stalled = True  # locked rotor => high current

    monitor.tick()  # first sight of it: start the grace timer, do not trip
    board.clock.advance(0.5)
    monitor.tick()
    board.clock.advance(0.6)
    with pytest.raises(SafetyAbort, match="drew") as exc:
        monitor.tick()
    assert exc.value.reason is StopReason.OVERCURRENT


def test_overcurrent_check_is_skipped_when_no_sensing_is_fitted(settings, board):
    monitor = SafetyMonitor(board, settings, EStopLatch())
    assert settings.safety.overcurrent_a == 0
    board.set_motor(MotorId.DRIVE, 100)
    board.state.stalled = True
    monitor.tick()  # no exception: there is nothing to measure with


def test_rain_mid_run_aborts_when_configured(settings, board):
    monitor = SafetyMonitor(board, settings, EStopLatch())
    assert settings.safety.rain_aborts_run
    board.state.raining = True
    with pytest.raises(SafetyAbort, match="mid-run"):
        monitor.tick()


def test_estop_input_latches_through_the_monitor(settings, board):
    latch = EStopLatch()
    monitor = SafetyMonitor(board, settings, latch)
    board.press_estop()
    with pytest.raises(SafetyAbort):
        monitor.tick()
    board.release_estop()
    assert board.read_input(InputId.ESTOP) is False
    with pytest.raises(SafetyAbort):  # still latched
        monitor.tick()
