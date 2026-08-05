"""End-to-end cycles against the simulator.

These are the tests that could not exist for v1 — not because nobody wrote
them, but because the code could not be imported, let alone run.
"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest
from conftest import make_robot

from solarsweep.control import CycleRefused, RobotState, SolarSweepRobot
from solarsweep.hal.sim import SimBoard
from solarsweep.safety import StopReason

# -- The happy path --------------------------------------------------------


def test_full_cycle_completes_and_ends_idle(robot):
    summary = robot.run_cycle("full_cycle")
    assert summary.outcome == "completed"
    assert summary.passes_completed == summary.passes_planned
    assert robot.machine.state is RobotState.IDLE


def test_a_completed_cycle_actually_covers_the_panel(robot, board):
    """The point of the machine. v1 could report a successful cycle while
    the carriage had travelled an arbitrary distance, because its traverse
    time came from dividing millimetres by a PWM duty cycle."""
    robot.run_cycle("full_cycle")
    assert board.coverage_pct > 95
    assert board.state.soiling < 0.1


def test_cycle_returns_the_carriage_home(robot, board):
    robot.run_cycle("quick_pass")
    assert board.state.position_mm == pytest.approx(0.0, abs=2.0)


def test_everything_is_de_energised_at_the_end(robot, board):
    robot.run_cycle("quick_pass")
    assert board.state.drive_duty == 0
    assert board.state.brush_duty == 0
    assert board.state.pump_on is False


def test_quick_pass_does_one_pass(robot):
    summary = robot.run_cycle("quick_pass")
    assert summary.passes_planned == 1
    assert summary.passes_completed == 1


def test_water_is_pulsed_not_left_on(robot, board):
    """v1 switched the pump on for the whole traverse and left the two pulse
    settings unused."""
    robot.run_cycle("quick_pass")
    assert robot.head.pulses > 1
    assert board.state.pump_on is False
    # Pulsed delivery uses far less than a continuously running pump would.
    continuous_ml = SimBoard.PUMP_ML_PER_S * 30
    assert board.state.water_dispensed_ml < continuous_ml


def test_passes_alternate_direction(robot, settings):
    """Odd passes sweep away from home, even ones sweep back, so the carriage
    cleans in both directions instead of deadheading."""
    settings_2 = replace(settings, cleaning=replace(settings.cleaning, passes=2))
    board = SimBoard(settings_2)
    bot = SolarSweepRobot(board, settings_2)
    try:
        summary = bot.run_cycle("full_cycle")
    finally:
        bot.monitor.stop()
    assert summary.passes_completed == 2
    expected = settings_2.cleaning_span_mm * 2
    assert summary.distance_mm == pytest.approx(expected, rel=0.1)


# -- Refusals (decisions, not faults) --------------------------------------


def test_second_cycle_too_soon_is_refused(robot):
    robot.run_cycle("quick_pass")
    summary = robot.run_cycle("quick_pass")
    assert summary.outcome == "refused"
    assert "minimum gap" in summary.detail
    assert robot.machine.state is RobotState.IDLE  # a refusal is not a fault


def test_clean_panel_is_skipped_when_a_dust_threshold_is_set(settings):
    settings = replace(settings, triggers=replace(settings.triggers,
                                                  dust_threshold=900))
    bot, board = make_robot(settings)
    try:
        summary = bot.run_cycle("full_cycle")
    finally:
        bot.monitor.stop()
    assert summary.outcome == "refused"
    assert "clean enough" in summary.detail


def test_unattended_run_is_refused_without_stall_protection(open_loop_settings):
    bot, _ = make_robot(open_loop_settings)
    bot.unattended = True
    try:
        summary = bot.run_cycle("full_cycle")
    finally:
        bot.monitor.stop()
    assert summary.outcome == "refused"
    assert "encoder" in summary.detail


def test_supervised_run_is_allowed_without_stall_protection(open_loop_settings):
    """Deliberately still permitted: the operator is standing there."""
    bot, _ = make_robot(open_loop_settings)
    try:
        summary = bot.run_cycle("quick_pass")
    finally:
        bot.monitor.stop()
    assert summary.outcome == "completed"


def test_unknown_mode_is_rejected(robot):
    with pytest.raises(CycleRefused):
        robot.run_cycle("scrub_really_hard")


def test_cannot_start_while_faulted(settings):
    bot, board = make_robot(settings, fault="obstacle@400")
    try:
        bot.run_cycle("quick_pass")
        assert bot.machine.state is RobotState.FAULTED
        with pytest.raises(CycleRefused, match="faulted"):
            bot.run_cycle("quick_pass")
    finally:
        bot.monitor.stop()


# -- Faults ----------------------------------------------------------------


@pytest.mark.parametrize(
    "fault, reason",
    [
        ("obstacle@500", StopReason.OBSTACLE),
        ("rain@2", StopReason.RAIN),
        ("estop@2", StopReason.ESTOP_BUTTON),
        ("drive_stall@400", StopReason.STALL),
        ("stuck_limit_far", StopReason.LIMIT_UNEXPECTED),
    ],
)
def test_each_fault_stops_the_cycle_with_the_right_reason(settings, fault, reason):
    bot, board = make_robot(settings, fault=fault)
    try:
        summary = bot.run_cycle("full_cycle")
        assert summary.outcome in ("aborted", "estopped")
        assert summary.stop_reason == reason.value
        # Whatever went wrong, the machine is off.
        assert board.state.drive_duty == 0
        assert board.state.brush_duty == 0
        assert board.state.pump_on is False
    finally:
        bot.monitor.stop()


def test_an_abort_mid_pass_stops_the_pump(settings):
    """Leaving a pump running after an abort is how you flood a roof."""
    bot, board = make_robot(settings, fault="obstacle@700")
    try:
        bot.run_cycle("full_cycle")
    finally:
        bot.monitor.stop()
    assert board.state.pump_on is False
    assert not bot.head.running


def test_estop_latches_and_survives_the_button_being_released(settings):
    bot, board = make_robot(settings, fault="estop@2")
    try:
        summary = bot.run_cycle("full_cycle")
        assert summary.outcome == "estopped"
        assert bot.machine.state is RobotState.ESTOPPED
        board.release_estop()
        assert bot.latch.tripped  # still latched
        assert bot.clear_fault()
        assert bot.machine.state is RobotState.IDLE
    finally:
        bot.monitor.stop()


def test_clear_fault_refuses_while_the_button_is_held(settings):
    bot, board = make_robot(settings, fault="estop@2")
    try:
        bot.run_cycle("full_cycle")
        assert bot.clear_fault() is False  # button still down
        board.release_estop()
        assert bot.clear_fault() is True
    finally:
        bot.monitor.stop()


def test_repeated_faults_latch_the_machine_out(settings):
    """A scheduler that retries forever will grind a gearbox to powder."""
    settings = replace(
        settings,
        safety=replace(settings.safety, max_consecutive_faults=2),
        triggers=replace(settings.triggers, min_hours_between_runs=0.0),
    )
    bot, board = make_robot(settings, fault="drive_stall@400")
    try:
        first = bot.run_cycle("full_cycle")
        assert first.outcome == "aborted"
        bot.machine.to(RobotState.IDLE)  # operator acknowledges
        second = bot.run_cycle("full_cycle")
        assert second.outcome == "estopped"
        assert bot.latch.tripped
    finally:
        bot.monitor.stop()


def test_no_water_degrades_to_a_dry_pass_rather_than_failing(settings):
    bot, board = make_robot(settings, fault="no_water")
    try:
        summary = bot.run_cycle("quick_pass")
    finally:
        bot.monitor.stop()
    assert summary.outcome == "completed"
    assert summary.water_pulses == 0
    assert board.state.water_dispensed_ml == 0


def test_an_unexpected_exception_still_leaves_the_machine_safe(robot, board,
                                                              monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("something nobody predicted")

    monkeypatch.setattr(robot.head, "start", explode)
    summary = robot.run_cycle("quick_pass")
    assert summary.outcome == "faulted"
    assert summary.stop_reason == "internal_error"
    assert board.state.drive_duty == 0
    assert board.state.pump_on is False


# -- Operator control ------------------------------------------------------


def test_pause_actually_stops_the_carriage_mid_run(settings):
    """v1's pause could only fire from CLEANING, while the traverse loop that
    read it always ran in TRAVERSING. It was unreachable code."""
    bot, board = make_robot(settings)
    positions: list[float] = []

    def pause_then_sample():
        # Let it get going, then hold it and watch it stay put.
        while board.state.position_mm < 300:
            threading.Event().wait(0.005)
        bot.pause()
        threading.Event().wait(0.05)
        positions.append(board.state.position_mm)
        threading.Event().wait(0.05)
        positions.append(board.state.position_mm)
        bot.resume()

    watcher = threading.Thread(target=pause_then_sample, daemon=True)
    watcher.start()
    try:
        summary = bot.run_cycle("full_cycle")
    finally:
        watcher.join(timeout=5)
        bot.monitor.stop()

    assert summary.outcome == "completed"
    assert len(positions) == 2
    assert positions[0] == pytest.approx(positions[1], abs=1.0)


def test_estop_from_the_api_stops_a_running_cycle(settings):
    bot, board = make_robot(settings)
    result: dict = {}

    def run():
        result["summary"] = bot.run_cycle("full_cycle")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    while board.state.position_mm < 200 and worker.is_alive():
        threading.Event().wait(0.005)
    bot.estop("test")
    worker.join(timeout=10)
    bot.monitor.stop()

    assert result["summary"].outcome == "estopped"
    assert board.state.drive_duty == 0


def test_resume_is_refused_while_latched(robot):
    robot.estop("test")
    robot.resume()
    assert robot.machine.state is RobotState.ESTOPPED


# -- Status ----------------------------------------------------------------


def test_status_reports_the_position_source(robot, open_loop_settings):
    assert robot.status["position_source"] == "encoder"
    bot, _ = make_robot(open_loop_settings)
    try:
        assert bot.status["position_source"] == "dead-reckoned"
        assert bot.status["stall_protection"] is False
    finally:
        bot.monitor.stop()


def test_status_is_json_serialisable(robot):
    import json

    robot.run_cycle("quick_pass")
    json.dumps(robot.status)  # must not raise
