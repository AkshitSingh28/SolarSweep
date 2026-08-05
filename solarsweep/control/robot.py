"""Cycle orchestration.

The shape of a cycle is deliberately boring: home, then alternate passes along
the rail, then park. What is not boring — and is the whole point of the
rewrite — is that every step is bounded, every interlock raises rather than
returns, and a fault leaves the machine in a state that requires a human to
acknowledge before it will move again.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from ..config import Settings
from ..hal import Board, InputId
from ..safety import EStopLatch, SafetyAbort, SafetyMonitor, StopReason
from ..telemetry import RunRecorder, RunSummary
from .axis import RailAxis
from .cleaning_head import CleaningHead
from .states import STARTABLE_STATES, PauseGate, RobotState, StateMachine

logger = logging.getLogger(__name__)


class CycleRefused(RuntimeError):
    """The robot declined to start. Not a fault — a decision."""


class SolarSweepRobot:
    def __init__(
        self,
        board: Board,
        settings: Settings,
        *,
        unattended: bool = False,
    ) -> None:
        self.settings = settings
        self.board = board
        self.unattended = unattended

        self.latch = EStopLatch()
        self.monitor = SafetyMonitor(board, settings, self.latch)
        self.pause_gate = PauseGate()
        self.axis = RailAxis(board, settings, self.monitor, self.pause_gate)
        self.head = CleaningHead(board, settings, self.monitor)
        self.machine = StateMachine(RobotState.IDLE)

        self._lock = threading.Lock()
        self._recorder: RunRecorder | None = None
        self._last_run_ended_at: float | None = None
        self._consecutive_faults = 0
        self._last_summary: RunSummary | None = None
        self._pass_index = 0
        self._passes_planned = 0

        self.machine.add_listener(self._on_state_change)

        if not self.axis.stall_protection_available:
            logger.warning(
                "No encoder and no current sensing: STALL DETECTION IS UNAVAILABLE. "
                "A jammed carriage will be caught only by the motion timeout "
                "(up to %.0fs of pushing). Fit an encoder before leaving this "
                "machine unattended — see docs/COMMISSIONING.md.",
                settings.safety.home_timeout_s,
            )

    # -- Public control ----------------------------------------------------

    def run_cycle(self, mode: str = "full_cycle") -> RunSummary:
        """Run one cleaning cycle to completion. Raises nothing on a safety
        abort — the outcome is in the returned summary."""
        with self._lock:
            if self.machine.state not in STARTABLE_STATES:
                raise CycleRefused(
                    f"cannot start a cycle from state {self.machine.state.value}"
                )
            if mode not in ("full_cycle", "quick_pass"):
                raise CycleRefused(f"unknown mode {mode!r}")
            self._passes_planned = (
                self.settings.cleaning.passes if mode == "full_cycle" else 1
            )
            recorder = RunRecorder(self.settings.logging.telemetry_dir, mode)
            self._recorder = recorder
            self._pass_index = 0

        recorder.summary.passes_planned = self._passes_planned
        recorder.event(
            "run_start",
            mode=mode,
            backend=self.board.info.backend,
            unattended=self.unattended,
            stall_protection=self.axis.stall_protection_available,
            settings={
                "passes": self._passes_planned,
                "rail_travel_mm": self.settings.panel.rail_travel_mm,
                "cleaning_duty_pct": self.settings.drive.cleaning_duty_pct,
            },
        )
        self.monitor.start()
        started = time.monotonic()

        try:
            self._preflight(recorder)
            self._home(recorder)
            for index in range(1, self._passes_planned + 1):
                self._pass_index = index
                self._run_pass(index, recorder)
                recorder.summary.passes_completed = index
            self._park(recorder)
            self._consecutive_faults = 0
            summary = self._finish(recorder, "completed")

        except CycleRefused as exc:
            self._safe_shutdown()
            self.machine.to(RobotState.IDLE)
            summary = self._finish(recorder, "refused", detail=str(exc))

        except SafetyAbort as exc:
            logger.error("cycle aborted: %s", exc)
            self._safe_shutdown()
            self._consecutive_faults += 1
            if exc.reason is StopReason.ESTOP_BUTTON or self.latch.tripped:
                self.machine.force(RobotState.ESTOPPED)
                outcome = "estopped"
            else:
                self.machine.force(RobotState.FAULTED)
                outcome = "aborted"
            if self._consecutive_faults >= self.settings.safety.max_consecutive_faults:
                # Something is wrong with the machine, not with this run.
                # Latch out so the scheduler stops re-triggering it.
                self.latch.trip(
                    exc.reason,
                    f"{self._consecutive_faults} consecutive faults; "
                    f"last: {exc.detail}",
                )
                self.machine.force(RobotState.ESTOPPED)
                outcome = "estopped"
            summary = self._finish(
                recorder, outcome, stop_reason=exc.reason.value, detail=exc.detail
            )

        except Exception as exc:  # unexpected: still leave the machine safe
            logger.exception("unexpected error during cycle")
            self._safe_shutdown()
            self.machine.force(RobotState.FAULTED)
            self._consecutive_faults += 1
            summary = self._finish(
                recorder, "faulted", stop_reason="internal_error", detail=repr(exc)
            )

        finally:
            self.monitor.stop()
            self.pause_gate.resume()
            self._last_run_ended_at = time.monotonic()
            with self._lock:
                self._recorder = None
            logger.info("cycle finished in %.1fs", time.monotonic() - started)

        self._last_summary = summary
        return summary

    def pause(self) -> None:
        if self.machine.state in (RobotState.FAULTED, RobotState.ESTOPPED):
            return
        self.pause_gate.pause()
        if self.machine.can(RobotState.PAUSED):
            self.machine.to(RobotState.PAUSED)
        self._record("paused")

    def resume(self) -> None:
        if self.latch.tripped:
            logger.warning("refusing to resume: e-stop is latched")
            return
        if self.machine.state is RobotState.PAUSED:
            self.machine.to(self.machine.previous)
        self.pause_gate.resume()
        self._record("resumed")

    def estop(self, detail: str = "operator request") -> None:
        """Latch the machine out and de-energise. Reachable from any state."""
        self.latch.trip(StopReason.OPERATOR, detail)
        self.pause_gate.resume()  # do not leave a paused loop spinning
        self._safe_shutdown()
        self.machine.force(RobotState.ESTOPPED)
        self._record("estop", detail=detail)

    def clear_fault(self) -> bool:
        """Acknowledge a fault. Refuses while the physical e-stop is held."""
        if self.board.read_input(InputId.ESTOP):
            logger.warning("cannot clear: the e-stop input is still asserted")
            return False
        self.latch.reset()
        self._consecutive_faults = 0
        if self.machine.state in (RobotState.FAULTED, RobotState.ESTOPPED):
            self.machine.to(RobotState.IDLE)
        self._record("fault_cleared")
        return True

    def stop(self) -> None:
        """De-energise every output without tearing the board down.

        Safe to call at any time and from any state, including twice in a row.
        """
        self._safe_shutdown()

    def close(self) -> None:
        self.monitor.stop()
        self._safe_shutdown()
        self.board.close()

    def __enter__(self) -> SolarSweepRobot:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- Cycle steps -------------------------------------------------------

    def _preflight(self, recorder: RunRecorder) -> None:
        self.machine.to(RobotState.PREFLIGHT)

        if self.unattended and not self.axis.stall_protection_available:
            raise CycleRefused(
                "unattended run refused: neither an encoder nor current sensing "
                "is configured, so a jammed carriage cannot be detected. Run "
                "manually with `solarsweep run` while watching it, or fit an "
                "encoder and set drive.encoder_ticks_per_mm."
            )

        gap_h = self.settings.triggers.min_hours_between_runs
        if gap_h > 0 and self._last_run_ended_at is not None:
            since_h = (time.monotonic() - self._last_run_ended_at) / 3600.0
            if since_h < gap_h:
                raise CycleRefused(
                    f"last cycle finished {since_h:.1f}h ago; minimum gap is {gap_h}h"
                )

        self.monitor.preflight()

        dust = self.board.read_dust()
        recorder.summary.dust_before = dust
        recorder.event("dust", stage="before", value=dust)
        threshold = self.settings.triggers.dust_threshold
        if threshold > 0 and dust < threshold:
            raise CycleRefused(
                f"panel is clean enough (dust {dust} < threshold {threshold}); "
                "skipping to save water and brush wear"
            )

    def _home(self, recorder: RunRecorder) -> None:
        self.machine.to(RobotState.HOMING)
        if self.settings.safety.reference_on_start and not self.axis.referenced:
            measured = self.axis.reference()
            recorder.event(
                "referenced",
                measured_travel_mm=round(measured, 1),
                configured_travel_mm=self.settings.panel.rail_travel_mm,
            )
        result = self.axis.home()
        recorder.event("homed", duration_s=round(result.duration_s, 2))

    def _run_pass(self, index: int, recorder: RunRecorder) -> None:
        panel = self.settings.panel
        near = panel.lead_in_mm
        far = panel.rail_travel_mm - panel.lead_out_mm
        # Odd passes sweep away from home, even passes sweep back. The carriage
        # therefore cleans in both directions instead of deadheading.
        start, end = (near, far) if index % 2 else (far, near)

        self.machine.to(RobotState.POSITIONING)
        move = self.axis.move_to(
            start, self.settings.drive.transit_duty_pct, f"positioning for pass {index}"
        )
        recorder.event(
            "positioned", **{"pass": index, "at_mm": round(move.end_mm, 1)}
        )

        self.machine.to(RobotState.CLEANING)
        self.head.start()
        try:
            sweep = self.axis.move_to(
                end,
                self.settings.drive.cleaning_duty_pct,
                f"cleaning pass {index}",
                on_tick=self.head.tick,
            )
        finally:
            # The brush and pump stop even if the traverse raised. Leaving a
            # pump running after an abort is how you flood a roof.
            self.head.stop()

        recorder.summary.distance_mm += sweep.distance_mm
        recorder.summary.water_pulses = self.head.pulses
        recorder.event(
            "pass_complete",
            **{
                "pass": index,
                "from_mm": round(sweep.start_mm, 1),
                "to_mm": round(sweep.end_mm, 1),
                "distance_mm": round(sweep.distance_mm, 1),
                "duration_s": round(sweep.duration_s, 2),
                "stopped_by": sweep.stopped_by,
                "water": self.head.water_enabled,
            },
        )

    def _park(self, recorder: RunRecorder) -> None:
        self.machine.to(RobotState.PARKING)
        # Transit most of the way at speed, then home the last stretch slowly.
        # Homing duty is deliberately low because it ends against a hard stop;
        # crawling the entire rail at that duty wastes minutes and can trip the
        # homing timeout on a long array.
        approach_mm = min(80.0, self.settings.panel.rail_travel_mm / 4)
        if self.axis.position_mm > approach_mm:
            self.axis.move_to(
                approach_mm, self.settings.drive.transit_duty_pct, "returning to park"
            )
        # Re-home rather than trusting the estimate: this is the one moment in
        # the cycle where a physical reference is free, and it bounds the
        # dead-reckoning error that accumulates over a run.
        result = self.axis.home()
        dust = self.board.read_dust()
        recorder.summary.dust_after = dust
        recorder.event("dust", stage="after", value=dust)
        recorder.event("parked", duration_s=round(result.duration_s, 2))
        self.machine.to(RobotState.IDLE)

    # -- Helpers -----------------------------------------------------------

    def _safe_shutdown(self) -> None:
        """Everything off. Must never raise — it runs from except blocks."""
        try:
            self.head.stop()
        except Exception:  # pragma: no cover
            logger.exception("cleaning head failed to stop")
        try:
            self.axis.stop()
        except Exception:  # pragma: no cover
            logger.exception("axis failed to stop")
        try:
            self.board.all_stop()
        except Exception:  # pragma: no cover
            logger.exception("board all_stop failed")

    def _finish(
        self,
        recorder: RunRecorder,
        outcome: str,
        stop_reason: str | None = None,
        detail: str = "",
    ) -> RunSummary:
        summary = recorder.finish(outcome, stop_reason, detail)
        level = logging.INFO if outcome == "completed" else logging.WARNING
        logger.log(
            level,
            "run %s: %s (%d/%d passes, %.0fmm, %d water pulses)%s",
            summary.run_id, outcome, summary.passes_completed,
            summary.passes_planned, summary.distance_mm, summary.water_pulses,
            f" — {detail}" if detail else "",
        )
        if recorder.path is not None:
            logger.info("telemetry: %s", recorder.path)
        return summary

    def _record(self, kind: str, **fields) -> None:
        with self._lock:
            recorder = self._recorder
        if recorder is not None:
            recorder.event(kind, state=self.machine.state.value, **fields)

    def _on_state_change(self, old: RobotState, new: RobotState) -> None:
        self._record("state", **{"from": old.value, "to": new.value})

    # -- Status ------------------------------------------------------------

    @property
    def status(self) -> dict:
        measured = self.axis.measured_position_mm
        return {
            "name": self.settings.name,
            "state": self.machine.state.value,
            "backend": self.board.info.backend,
            "simulated": self.board.info.simulated,
            "position_mm": round(self.axis.position_mm, 1),
            "position_source": "encoder" if measured is not None else "dead-reckoned",
            "rail_travel_mm": self.settings.panel.rail_travel_mm,
            # The dashboard draws the rail to scale, so it needs the geometry
            # as well as the position.
            "lead_in_mm": self.settings.panel.lead_in_mm,
            "lead_out_mm": self.settings.panel.lead_out_mm,
            "homed": self.axis.homed,
            "referenced": self.axis.referenced,
            "paused": self.pause_gate.paused,
            "pass": self._pass_index,
            "passes_planned": self._passes_planned,
            "brush_running": self.head.running,
            "water_enabled": self.head.water_enabled,
            "pump_on": self.head.pump_on,
            "water_pulses": self.head.pulses,
            "stall_protection": self.axis.stall_protection_available,
            "consecutive_faults": self._consecutive_faults,
            "estop": self.latch.snapshot(),
            "inputs": {
                "limit_home": self.board.read_input(InputId.LIMIT_HOME),
                "limit_far": self.board.read_input(InputId.LIMIT_FAR),
                "estop": self.board.read_input(InputId.ESTOP),
                "rain": self.board.read_input(InputId.RAIN),
                "obstacle": self.board.read_input(InputId.OBSTACLE),
                "water_pressure": self.board.read_input(InputId.WATER_PRESSURE),
            },
            "dust": self.board.read_dust(),
            "last_run": (
                {
                    "run_id": self._last_summary.run_id,
                    "outcome": self._last_summary.outcome,
                    "stop_reason": self._last_summary.stop_reason,
                    "detail": self._last_summary.detail,
                    "passes": self._last_summary.passes_completed,
                    "duration_s": self._last_summary.duration_s,
                }
                if self._last_summary
                else None
            ),
        }

    @property
    def telemetry_dir(self) -> Path:
        return Path(self.settings.logging.telemetry_dir)
