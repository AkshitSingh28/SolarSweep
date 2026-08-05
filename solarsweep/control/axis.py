"""The rail axis: the one degree of freedom the machine actually has.

A note on what changed from v1, because it is the single most important
correction in this rewrite:

v1 modelled two axes — X along the rail and a Z "lift" that raised the chassis
on steel rods before each pass. **The built machine has no Z axis.** The
photographs show a carriage that rides on two round rails with spring-loaded
posts at four corners; the springs hold the brush against the glass passively.
There is nothing to lift. Half of v1's state machine drove a motor that does
not exist, and would have blocked forever waiting for a ``limit_top`` switch
that was never fitted.

The second correction is about position. v1 computed ``duration = distance /
speed`` where ``speed`` was then handed to the motor as a **PWM duty cycle**.
Those are different physical quantities; the resulting travel distance was
whatever it happened to be. Here:

* if an encoder is fitted, position is *measured*;
* if not, position is dead-reckoned from a **calibrated** mm/s figure and
  re-zeroed at every limit switch, and the code says out loud that stall
  detection is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from ..hal import Board, InputId, MotorId
from ..safety import Deadline, SafetyAbort, SafetyMonitor, StopReason

logger = logging.getLogger(__name__)

#: Control period. 50 Hz is comfortable for a Pi Zero 2 W and fine for a
#: carriage that moves at a few cm/s.
TICK_S = 0.02


class Direction:
    HOME = -1
    FAR = +1


@dataclass
class MoveResult:
    start_mm: float
    end_mm: float
    duration_s: float
    stopped_by: str  # "target" | "limit"
    #: Position before any limit-switch correction was applied. This is the
    #: number the referencing run measures the rail with; ``end_mm`` has
    #: already been snapped to the configured travel.
    raw_end_mm: float = 0.0

    @property
    def distance_mm(self) -> float:
        return abs(self.end_mm - self.start_mm)


class RailAxis:
    def __init__(
        self,
        board: Board,
        settings: Settings,
        monitor: SafetyMonitor,
        pause_gate=None,
    ) -> None:
        self._board = board
        self._settings = settings
        self._drive = settings.drive
        self._panel = settings.panel
        self._monitor = monitor
        self._pause_gate = pause_gate
        self._clock = board.clock
        self._estimate_mm = 0.0
        self._encoder_zero_mm = 0.0
        self._homed = False
        self._referenced = False
        self._duty = 0.0

    # -- Position ----------------------------------------------------------

    @property
    def position_mm(self) -> float:
        """Best available estimate, measured where possible."""
        measured = self.measured_position_mm
        return measured if measured is not None else self._estimate_mm

    @property
    def measured_position_mm(self) -> float | None:
        """Position from the encoder, or None when no encoder is fitted.

        Only this value is trustworthy enough to detect a stall — a
        dead-reckoned estimate happily keeps counting up while the carriage
        sits pinned against an end stop, which is exactly the situation stall
        detection exists to catch.
        """
        if not self._drive.closed_loop:
            return None
        delta = self._board.read_encoder_mm()
        if delta is None:
            return None
        return self._encoder_zero_mm + delta

    @property
    def homed(self) -> bool:
        return self._homed

    @property
    def stall_protection_available(self) -> bool:
        return self._drive.closed_loop or self._settings.safety.overcurrent_a > 0

    def _set_position(self, value: float) -> None:
        self._estimate_mm = value
        self._encoder_zero_mm = value
        self._board.reset_encoder()

    # -- Motion primitives -------------------------------------------------

    def home(self) -> MoveResult:
        """Drive toward the home stop until the switch asserts.

        Bounded by ``safety.home_timeout_s``. v1's equivalent was
        ``while not limit_rear(): sleep(0.05)`` with no exit — a dead switch
        meant the motor pushed until something gave.
        """
        logger.info("Homing rail axis")
        if self._board.read_input(InputId.LIMIT_HOME):
            # Already on the switch. Back off so homing ends on a clean edge
            # rather than an ambiguous "was it already pressed?" state.
            self._nudge(Direction.FAR, 25.0)

        result = self._run_motion(
            direction=Direction.HOME,
            duty_pct=self._drive.homing_duty_pct,
            timeout_s=self._settings.safety.home_timeout_s,
            purpose="homing to the near end stop",
            should_stop=lambda: self._board.read_input(InputId.LIMIT_HOME),
            stop_label="limit",
        )
        self._set_position(0.0)
        self._homed = True
        logger.info("Homed in %.1fs", result.duration_s)
        return MoveResult(result.start_mm, 0.0, result.duration_s, "limit", 0.0)

    def reference(self) -> float:
        """Home, then run to the far stop to prove both switches and measure
        the rail.

        Worth the extra traverse for two reasons. First, a cleaning pass stops
        on a *position* target well short of the far switch, so a dead far
        switch is never exercised during normal operation and would sit
        undetected until the day something else went wrong. Second, every
        open-loop distance in this program is relative to
        ``panel.rail_travel_mm`` — a number that starts life as somebody's
        estimate. This measures it.

        Returns the measured travel in mm.
        """
        logger.info("Referencing: proving both limit switches and measuring the rail")
        self.home()

        budget_mm = self._panel.rail_travel_mm * 1.25
        timeout = self._timeout_for(budget_mm, self._drive.transit_duty_pct)
        try:
            result = self._run_motion(
                direction=Direction.FAR,
                duty_pct=self._drive.transit_duty_pct,
                timeout_s=timeout,
                purpose="referencing to the far end stop",
                should_stop=lambda: False,  # only the limit switch ends this
                stop_label="limit",
            )
        except SafetyAbort as exc:
            # A stall *at the far end* means the carriage arrived and the
            # switch did not fire. A stall halfway down the rail means
            # something is jammed, and saying "check the far limit switch"
            # would send whoever is debugging it to the wrong end of the
            # machine.
            reached = self.position_mm
            near_far_end = reached >= self._panel.rail_travel_mm * 0.9
            if exc.reason in (StopReason.TIMEOUT, StopReason.STALL) and near_far_end:
                raise SafetyAbort(
                    StopReason.LIMIT_UNEXPECTED,
                    "the far limit switch never asserted during the referencing run "
                    f"({exc.detail}). The carriage reached {reached:.0f}mm of a "
                    f"configured {self._panel.rail_travel_mm:.0f}mm, so it is at the "
                    "far end but the switch is not reporting it. Check the switch, "
                    "its wiring and its mounting bracket — without it the only thing "
                    "stopping the carriage at the far end is the frame.",
                ) from exc
            raise

        measured = result.raw_end_mm
        configured = self._panel.rail_travel_mm
        error_pct = abs(measured - configured) / configured * 100.0
        tolerance = self._settings.safety.rail_travel_tolerance_pct

        if error_pct > tolerance:
            raise SafetyAbort(
                StopReason.CONFIG,
                f"measured rail travel is {measured:.0f}mm but "
                f"panel.rail_travel_mm says {configured:.0f}mm ({error_pct:.0f}% out, "
                f"tolerance {tolerance:.0f}%). Either the setting is wrong or "
                f"drive.duty_to_mmps needs recalibrating — run `solarsweep calibrate`.",
            )

        logger.info(
            "Referenced: measured %.0fmm travel (configured %.0fmm, %.1f%% out)",
            measured, configured, error_pct,
        )
        self._referenced = True
        return measured

    @property
    def referenced(self) -> bool:
        return self._referenced

    def move_to(
        self, target_mm: float, duty_pct: float, purpose: str, on_tick=None
    ) -> MoveResult:
        """Move to an absolute position along the rail.

        ``on_tick`` runs once per control period while moving — the cleaning
        head uses it to advance its water pulse schedule.
        """
        if not self._homed:
            raise SafetyAbort(
                StopReason.CONFIG, "refusing to move before the axis has been homed"
            )
        target = max(0.0, min(self._panel.rail_travel_mm, float(target_mm)))
        start = self.position_mm
        delta = target - start
        if abs(delta) < 1.0:
            return MoveResult(start, start, 0.0, "target")

        direction = Direction.FAR if delta > 0 else Direction.HOME
        timeout = self._timeout_for(abs(delta), duty_pct)

        def reached() -> bool:
            pos = self.position_mm
            return pos >= target if direction == Direction.FAR else pos <= target

        return self._run_motion(
            direction=direction,
            duty_pct=duty_pct,
            timeout_s=timeout,
            purpose=purpose,
            should_stop=reached,
            stop_label="target",
            on_tick=on_tick,
        )

    def _nudge(self, direction: int, distance_mm: float) -> None:
        """Short open-loop move used to get off a limit switch."""
        duty = self._drive.homing_duty_pct
        seconds = distance_mm / max(1e-6, self._drive.duty_to_mmps * duty / 100.0)
        deadline = Deadline(self._clock, seconds + 2.0, "backing off the end stop")
        self._board.set_motor(MotorId.DRIVE, direction * duty)
        try:
            while not deadline.expired and deadline.elapsed < seconds:
                self._monitor.tick(self.measured_position_mm, moving=False)
                self._clock.sleep(TICK_S)
                self._integrate(direction * duty, TICK_S)
        finally:
            self.stop()

    # -- The one motion loop everything funnels through -------------------

    def _run_motion(
        self,
        *,
        direction: int,
        duty_pct: float,
        timeout_s: float,
        purpose: str,
        should_stop,
        stop_label: str,
        on_tick=None,
    ) -> MoveResult:
        deadline = Deadline(self._clock, timeout_s, purpose)
        start = self.position_mm
        target_duty = direction * duty_pct
        stopped_by = stop_label

        self._arm_stall()

        logger.debug(
            "motion: %s, dir=%+d duty=%.0f%% timeout=%.1fs from %.1fmm",
            purpose, direction, duty_pct, timeout_s, start,
        )

        try:
            while True:
                self._monitor.tick(self.measured_position_mm, moving=True)
                deadline.check()
                self._check_wrong_limit(direction)

                if self._pause_gate is not None and self._pause_gate.paused:
                    # Stop first, ask questions later: a paused machine holds
                    # position rather than coasting down the rail.
                    self.stop()
                    self._monitor.stall.disarm()
                    paused_s = self._pause_gate.wait(
                        lambda: self._monitor.tick(moving=False), self._clock
                    )
                    deadline.extend(paused_s)
                    self._arm_stall()
                    continue

                if should_stop():
                    break
                if self._hit_end_stop(direction):
                    stopped_by = "limit"
                    break

                self._duty = self._ramp(self._duty, target_duty)
                self._board.set_motor(MotorId.DRIVE, self._duty)
                self._clock.sleep(TICK_S)
                self._integrate(self._duty, TICK_S)
                if on_tick is not None:
                    on_tick()
        finally:
            self._monitor.stall.disarm()
            self.stop()

        end = raw_end = self.position_mm
        # A limit switch is ground truth: snap the estimate to it and discard
        # whatever dead-reckoning drift had accumulated. raw_end keeps the
        # pre-correction value so the referencing run can measure the rail.
        if stopped_by == "limit" and direction == Direction.FAR:
            self._set_position(self._panel.rail_travel_mm)
            end = self._panel.rail_travel_mm
        elif stopped_by == "limit" and direction == Direction.HOME:
            self._set_position(0.0)
            end = 0.0

        return MoveResult(start, end, deadline.elapsed, stopped_by, raw_end)

    def _arm_stall(self) -> None:
        """Stall detection needs a *measured* position; without an encoder it
        is simply unavailable and the deadline is the only backstop."""
        measured = self.measured_position_mm
        if measured is not None:
            self._monitor.stall.arm(measured)
        else:
            self._monitor.stall.disarm()

    def _ramp(self, current: float, target: float) -> float:
        """Ease the duty toward the target instead of stepping it.

        A gearmotor slammed from 0 to 75% duty jerks the carriage, which on a
        spring-mounted brush means the pads skip. It is also how you strip a
        plastic gear.
        """
        if self._drive.ramp_time_s <= 0:
            return target
        max_delta = 100.0 * TICK_S / self._drive.ramp_time_s
        if target > current:
            return min(target, current + max_delta)
        return max(target, current - max_delta)

    def _integrate(self, duty: float, dt: float) -> None:
        """Dead reckoning. Only meaningful without an encoder, and only
        because ``duty_to_mmps`` is a measured constant (see `calibrate`)."""
        if abs(duty) < self._drive.min_duty_pct:
            return
        self._estimate_mm += (duty / 100.0) * self._drive.duty_to_mmps * dt
        # Allow the estimate past the configured travel: over-running the far
        # end is exactly the symptom the referencing run looks for, and
        # clamping it here would hide it.
        self._estimate_mm = max(
            0.0, min(self._panel.rail_travel_mm * 1.3, self._estimate_mm)
        )

    def _hit_end_stop(self, direction: int) -> bool:
        channel = InputId.LIMIT_FAR if direction == Direction.FAR else InputId.LIMIT_HOME
        return self._board.read_input(channel)

    def _check_wrong_limit(self, direction: int) -> None:
        """The switch at the end we are moving *away* from should not fire.

        If it does, the switches are swapped or one is shorted. Both are
        wiring faults that would otherwise present as bizarre motion.
        """
        opposite = InputId.LIMIT_HOME if direction == Direction.FAR else InputId.LIMIT_FAR
        if self._board.read_input(opposite) and abs(self._duty) > 0:
            # Tolerate it while still physically on the switch we started from.
            near_home = self.position_mm < 20.0
            near_far = self.position_mm > self._panel.rail_travel_mm - 20.0
            if (opposite is InputId.LIMIT_HOME and near_home) or (
                opposite is InputId.LIMIT_FAR and near_far
            ):
                return
            raise SafetyAbort(
                StopReason.LIMIT_UNEXPECTED,
                f"{opposite.value} asserted while driving {'far' if direction > 0 else 'home'} "
                f"at {self.position_mm:.0f}mm — check for swapped or shorted limit switches",
            )

    def _timeout_for(self, distance_mm: float, duty_pct: float) -> float:
        """Bound a move by what it *should* take, plus margin."""
        mmps = max(1e-6, self._drive.duty_to_mmps * duty_pct / 100.0)
        predicted = distance_mm / mmps + self._drive.ramp_time_s
        limit = predicted * self._settings.safety.traverse_timeout_factor
        return min(limit, self._settings.safety.traverse_timeout_max_s)

    def stop(self) -> None:
        self._duty = 0.0
        self._board.brake_motor(MotorId.DRIVE)
        self._board.set_motor(MotorId.DRIVE, 0.0)
