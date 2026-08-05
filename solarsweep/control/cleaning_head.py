"""Brush and water.

Two behaviours here that v1 declared in ``settings.yaml`` and then never
implemented — ``water_pulse_ms`` and ``water_interval_s`` were read into a
dataclass and referenced nowhere, while the pump was simply switched on for
the whole pass:

* **Pulsed water.** A tethered supply on a roof is finite, and a continuously
  wet panel dries with mineral streaks that are worse than the dust. The pump
  runs in short bursts on a fixed cadence and the brush spreads it.
* **Spin-up before travel.** Starting the traverse before the brush is turning
  drags a stationary pad across dry glass. That is how you grind grit into an
  anti-reflective coating.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..hal import Board, InputId, MotorId, RelayId
from ..safety import SafetyMonitor

logger = logging.getLogger(__name__)


class CleaningHead:
    def __init__(self, board: Board, settings: Settings, monitor: SafetyMonitor) -> None:
        self._board = board
        self._cfg = settings.cleaning
        self._monitor = monitor
        self._clock = board.clock
        self._running = False
        self._pump_on = False
        self._last_pulse_start = 0.0
        self._water_enabled = False
        self.pulses = 0

    @property
    def running(self) -> bool:
        return self._running

    @property
    def water_enabled(self) -> bool:
        return self._water_enabled

    def start(self, *, allow_water: bool = True) -> None:
        """Spin the brush up, then decide whether water is available."""
        self._board.set_motor(MotorId.BRUSH, self._cfg.brush_duty_pct)
        self._running = True

        self._water_enabled = allow_water and not self._cfg.dry_pass
        if self._water_enabled and not self._board.read_input(InputId.WATER_PRESSURE):
            # Running a pump dry burns it out, and a dry brush on a dusty
            # panel scratches. Degrade to a dry pass rather than failing the
            # whole cycle, but say so loudly.
            logger.warning(
                "No water pressure detected — continuing as a DRY pass. "
                "Check the supply tap and the inline filter."
            )
            self._water_enabled = False

        logger.info(
            "Cleaning head started: brush %d%%, water %s",
            self._cfg.brush_duty_pct,
            "pulsed" if self._water_enabled else "off",
        )
        self._wait_for_spinup()

    def _wait_for_spinup(self) -> None:
        deadline = self._clock.now() + self._cfg.brush_spinup_s
        while self._clock.now() < deadline:
            self._monitor.tick(moving=False)
            self._clock.sleep(0.02)

    def tick(self) -> None:
        """Advance the water pulse schedule. Called from the traverse loop."""
        if not self._running or not self._water_enabled:
            return
        now = self._clock.now()
        pulse_s = self._cfg.water_pulse_ms / 1000.0

        if self._pump_on:
            if now - self._last_pulse_start >= pulse_s:
                self._set_pump(False)
        elif now - self._last_pulse_start >= self._cfg.water_interval_s:
            self._last_pulse_start = now
            self._set_pump(True)
            self.pulses += 1

    def _set_pump(self, on: bool) -> None:
        if on == self._pump_on:
            return
        self._board.set_relay(RelayId.PUMP, on)
        self._pump_on = on
        logger.debug("pump %s", "ON" if on else "OFF")

    def stop(self) -> None:
        """Idempotent, and safe to call from an exception handler."""
        self._set_pump(False)
        self._board.set_motor(MotorId.BRUSH, 0.0)
        self._board.brake_motor(MotorId.BRUSH)
        self._running = False
        self._water_enabled = False
