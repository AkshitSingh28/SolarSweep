"""Rail motion and the state machine."""

from __future__ import annotations

from dataclasses import replace

import pytest

from solarsweep.control.axis import RailAxis
from solarsweep.control.states import (
    IllegalTransition,
    PauseGate,
    RobotState,
    StateMachine,
)
from solarsweep.hal import MotorId
from solarsweep.hal.sim import FaultSpec, SimBoard
from solarsweep.safety import EStopLatch, SafetyAbort, SafetyMonitor, StopReason


def make_axis(settings) -> tuple[RailAxis, SimBoard]:
    board = SimBoard(settings)
    monitor = SafetyMonitor(board, settings, EStopLatch())
    return RailAxis(board, settings, monitor), board


# -- Homing and motion -----------------------------------------------------


def test_home_finds_the_switch_and_zeroes_the_position(settings):
    axis, board = make_axis(settings)
    board.place_carriage(900.0)
    result = axis.home()
    assert result.stopped_by == "limit"
    assert axis.position_mm == pytest.approx(0.0, abs=0.5)
    assert axis.homed


def test_home_times_out_instead_of_pushing_forever(settings):
    """v1: `while not limit_rear(): sleep(0.05)` — no exit, motor driving."""
    settings = replace(settings, sim=replace(settings.sim, fault="stuck_limit_home"))
    axis, board = make_axis(settings)
    board.place_carriage(900.0)
    with pytest.raises(SafetyAbort) as exc:
        axis.home()
    assert exc.value.reason in (StopReason.STALL, StopReason.TIMEOUT)
    assert board.state.drive_duty == 0  # and the motor is off afterwards


def test_move_before_homing_is_refused(settings):
    axis, _ = make_axis(settings)
    with pytest.raises(SafetyAbort, match="before the axis has been homed"):
        axis.move_to(500, 50, "nope")


def test_move_to_lands_on_target(settings):
    axis, _ = make_axis(settings)
    axis.home()
    result = axis.move_to(800.0, settings.drive.transit_duty_pct, "test move")
    assert result.stopped_by == "target"
    assert axis.position_mm == pytest.approx(800.0, abs=15.0)


def test_move_to_is_clamped_to_the_rail(settings):
    axis, _ = make_axis(settings)
    axis.home()
    axis.move_to(99_999.0, settings.drive.transit_duty_pct, "overshoot")
    assert axis.position_mm <= settings.panel.rail_travel_mm + 1


def test_motion_stops_the_motor_even_when_it_raises(settings):
    settings = replace(settings, sim=replace(settings.sim, fault="drive_stall@300"))
    axis, board = make_axis(settings)
    axis.home()
    with pytest.raises(SafetyAbort):
        axis.move_to(1500, settings.drive.transit_duty_pct, "will stall")
    assert board.state.drive_duty == 0


def test_duty_is_ramped_not_stepped(settings):
    """A gearmotor slammed to full duty jerks the carriage and strips gears."""
    axis, board = make_axis(settings)
    axis.home()
    seen: list[float] = []
    original = board.set_motor

    def spy(motor, duty):
        if motor is MotorId.DRIVE:
            seen.append(duty)
        original(motor, duty)

    board.set_motor = spy
    axis.move_to(600, 75, "ramped move")
    ramping = [d for d in seen if 0 < d < 75]
    assert len(ramping) > 5, f"expected a ramp, saw {seen[:10]}"


# -- Referencing -----------------------------------------------------------


def test_referencing_measures_the_rail(settings):
    axis, _ = make_axis(settings)
    measured = axis.reference()
    assert measured == pytest.approx(settings.panel.rail_travel_mm, rel=0.05)
    assert axis.referenced


def test_referencing_catches_a_dead_far_switch(settings):
    """A cleaning pass stops on a position target and never touches the far
    switch, so without this run a dead switch stays invisible."""
    settings = replace(settings, sim=replace(settings.sim, fault="stuck_limit_far"))
    axis, _ = make_axis(settings)
    with pytest.raises(SafetyAbort, match="far limit switch never asserted") as exc:
        axis.reference()
    assert exc.value.reason is StopReason.LIMIT_UNEXPECTED


def test_referencing_rejects_a_rail_length_that_disagrees_with_the_config(settings):
    """The realistic version of this failure: somebody typed the panel spec
    into rail_travel_mm instead of measuring switch to switch."""
    real_rail = replace(settings, panel=replace(settings.panel, rail_travel_mm=1200.0))
    real_rail.validate()
    board = SimBoard(real_rail)  # the rail is actually 1200mm...

    monitor = SafetyMonitor(board, settings, EStopLatch())
    axis = RailAxis(board, settings, monitor)  # ...but the config claims 1650mm

    with pytest.raises(SafetyAbort, match="measured rail travel") as exc:
        axis.reference()
    assert exc.value.reason is StopReason.CONFIG
    assert "solarsweep calibrate" in exc.value.detail


# -- Open loop -------------------------------------------------------------


def test_open_loop_reports_no_stall_protection(open_loop_settings):
    axis, _ = make_axis(open_loop_settings)
    assert axis.measured_position_mm is None
    assert not axis.stall_protection_available


def test_open_loop_still_homes_on_the_switch(open_loop_settings):
    axis, board = make_axis(open_loop_settings)
    board.place_carriage(700.0)
    axis.home()
    assert axis.position_mm == pytest.approx(0.0, abs=1.0)


def test_limit_switch_corrects_dead_reckoning_drift(open_loop_settings):
    """Dead reckoning drifts because the model ignores rail friction; the
    switch is ground truth and re-zeroes it."""
    axis, board = make_axis(open_loop_settings)
    axis.home()
    axis.move_to(1500, open_loop_settings.drive.transit_duty_pct, "out")
    drift = abs(axis.position_mm - board.state.position_mm)
    assert drift > 1.0, "expected some open-loop drift in the model"
    axis.home()
    assert axis.position_mm == 0.0
    assert board.state.position_mm == pytest.approx(0.0, abs=2.0)


# -- Wiring faults ---------------------------------------------------------


def test_wrong_limit_asserting_mid_travel_is_a_wiring_fault(settings):
    axis, board = make_axis(settings)
    axis.home()

    real_read = board.read_input

    def lying_read(channel):
        from solarsweep.hal import InputId

        if channel is InputId.LIMIT_HOME and board.state.position_mm > 300:
            return True  # home switch asserting in the middle of the rail
        return real_read(channel)

    board.read_input = lying_read
    with pytest.raises(SafetyAbort, match="swapped or shorted") as exc:
        axis.move_to(1500, settings.drive.transit_duty_pct, "away from home")
    assert exc.value.reason is StopReason.LIMIT_UNEXPECTED


# -- State machine ---------------------------------------------------------


def test_illegal_transitions_are_rejected():
    machine = StateMachine(RobotState.IDLE)
    machine.to(RobotState.PREFLIGHT)
    with pytest.raises(IllegalTransition):
        machine.to(RobotState.CLEANING)  # must position first


def test_a_fault_cannot_quietly_become_idle():
    machine = StateMachine(RobotState.IDLE)
    machine.force(RobotState.FAULTED)
    with pytest.raises(IllegalTransition):
        machine.to(RobotState.CLEANING)
    machine.to(RobotState.IDLE)  # only after acknowledgement


def test_estop_is_reachable_from_every_state():
    for state in RobotState:
        machine = StateMachine(state)
        machine.force(RobotState.ESTOPPED)
        assert machine.state is RobotState.ESTOPPED


def test_pause_remembers_where_it_came_from():
    machine = StateMachine(RobotState.IDLE)
    machine.to(RobotState.HOMING)
    machine.to(RobotState.POSITIONING)
    machine.to(RobotState.CLEANING)
    machine.to(RobotState.PAUSED)
    assert machine.previous is RobotState.CLEANING
    machine.to(machine.previous)
    assert machine.state is RobotState.CLEANING


def test_state_listeners_do_not_break_control():
    machine = StateMachine(RobotState.IDLE)
    machine.add_listener(lambda old, new: 1 / 0)
    machine.to(RobotState.PREFLIGHT)  # must not raise
    assert machine.state is RobotState.PREFLIGHT


def test_pause_gate_releases_and_reports_elapsed(settings):
    import threading

    board = SimBoard(settings)
    gate = PauseGate()
    gate.pause()
    threading.Timer(0.01, gate.resume).start()
    paused = gate.wait(lambda: None, board.clock)
    assert paused >= 0.0
    assert not gate.paused


# -- Fault spec parsing ----------------------------------------------------


def test_fault_spec_parses_names_and_thresholds():
    specs = FaultSpec.parse("rain@30, stuck_limit_far")
    assert specs[0].name == "rain" and specs[0].value == 30.0
    assert specs[1].name == "stuck_limit_far" and specs[1].value is None


def test_unknown_fault_name_is_rejected():
    with pytest.raises(ValueError, match="unknown fault"):
        FaultSpec.parse("explode")


def test_non_numeric_threshold_is_rejected():
    with pytest.raises(ValueError, match="not a number"):
        FaultSpec.parse("rain@soon")
