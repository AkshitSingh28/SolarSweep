"""Safety interlocks.

v1 had a method called ``emergency_stop`` that logged a message, set an enum,
and returned. Its callers ignored the return value and carried on cleaning.
There was no timeout on any motion, so a limit switch with a broken wire meant
the drive motor pushed the carriage into a hard stop until someone noticed.

Everything here exists to make that class of mistake structurally impossible:

* :class:`EStopLatch` — once tripped, *stays* tripped. Clearing it is an
  explicit operator action, not something a retry loop can do by accident.
* :class:`Deadline` — no loop waits forever. Every wait has a bound and an
  explanation of what it was waiting for.
* :class:`StallDetector` — if the carriage is commanded to move and doesn't,
  cut power. This is the last line of defence when a limit switch lies.
* :class:`Watchdog` — if the control loop stops ticking, kill the outputs from
  a separate thread.
* :class:`SafetyMonitor` — polls the interlocks once per control tick and
  raises. Raising is the point: it cannot be ignored the way a return value can.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from .hal import Board, Clock, InputId

logger = logging.getLogger(__name__)


class StopReason(str, Enum):
    ESTOP_BUTTON = "estop_button"
    OPERATOR = "operator"
    OBSTACLE = "obstacle"
    RAIN = "rain"
    STALL = "stall"
    OVERCURRENT = "overcurrent"
    TIMEOUT = "timeout"
    WATCHDOG = "watchdog"
    LIMIT_UNEXPECTED = "limit_unexpected"
    CONFIG = "config"


class SafetyAbort(Exception):
    """Raised the moment an interlock trips. Never caught to "continue anyway".

    Cycle code may catch this to record telemetry and shut down cleanly, but
    must re-raise or transition to a fault state — never resume motion.
    """

    def __init__(self, reason: StopReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


class EStopLatch:
    """A trip that persists until an operator explicitly clears it.

    The latch is the difference between "we stopped" and "we are stopped".
    Motion commands consult it on every tick, so a trip that happens while the
    carriage is mid-traverse takes effect on the next tick rather than at the
    end of the move.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tripped = False
        self._reason: StopReason | None = None
        self._detail = ""
        self._tripped_at: float | None = None

    def trip(self, reason: StopReason, detail: str = "") -> None:
        with self._lock:
            if self._tripped:
                return  # keep the *first* reason; it is the interesting one
            self._tripped = True
            self._reason = reason
            self._detail = detail
            self._tripped_at = time.time()
        logger.critical("E-STOP LATCHED: %s %s", reason.value, detail)

    def reset(self) -> None:
        with self._lock:
            was = self._reason
            self._tripped = False
            self._reason = None
            self._detail = ""
            self._tripped_at = None
        if was is not None:
            logger.warning("E-stop latch cleared (was %s)", was.value)

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped

    @property
    def reason(self) -> StopReason | None:
        with self._lock:
            return self._reason

    def raise_if_tripped(self) -> None:
        with self._lock:
            if self._tripped:
                assert self._reason is not None
                raise SafetyAbort(self._reason, self._detail)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "tripped": self._tripped,
                "reason": self._reason.value if self._reason else None,
                "detail": self._detail,
                "tripped_at": self._tripped_at,
            }


@dataclass
class Deadline:
    """A bounded wait with a human-readable purpose.

    Constructed from a Clock so the simulator and the tests can blow through
    a 90-second homing timeout in microseconds.
    """

    clock: Clock
    limit_s: float
    what: str
    _start: float = field(init=False)

    def __post_init__(self) -> None:
        self._start = self.clock.now()

    @property
    def elapsed(self) -> float:
        return self.clock.now() - self._start

    def extend(self, seconds: float) -> None:
        """Give back time that was not spent moving.

        Used when an operator pauses mid-traverse: the move gets its full
        allowance of *motion* time, but a pause still cannot be used to
        disable the timeout, because only measured paused time is returned.
        """
        if seconds > 0:
            self._start += seconds

    @property
    def expired(self) -> bool:
        return self.elapsed >= self.limit_s

    def check(self) -> None:
        if self.expired:
            raise SafetyAbort(
                StopReason.TIMEOUT,
                f"{self.what} did not finish within {self.limit_s:.1f}s",
            )


class StallDetector:
    """Cut power when commanded motion does not produce actual motion.

    Sampled against the best position estimate available — encoder if fitted,
    otherwise the dead-reckoned estimate, which still catches the case that
    matters most: the carriage pinned against a hard stop because a limit
    switch never asserted.
    """

    def __init__(self, clock: Clock, window_s: float, min_travel_mm: float) -> None:
        self._clock = clock
        self._window_s = window_s
        self._min_travel_mm = min_travel_mm
        self._anchor_pos = 0.0
        self._anchor_t = clock.now()
        self._armed = False

    def arm(self, position_mm: float) -> None:
        """Start watching. Called when motion is commanded."""
        self._anchor_pos = position_mm
        self._anchor_t = self._clock.now()
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    def update(self, position_mm: float) -> None:
        if not self._armed:
            return
        moved = abs(position_mm - self._anchor_pos)
        if moved >= self._min_travel_mm:
            # Progress. Re-anchor and keep watching.
            self._anchor_pos = position_mm
            self._anchor_t = self._clock.now()
            return
        if self._clock.now() - self._anchor_t >= self._window_s:
            raise SafetyAbort(
                StopReason.STALL,
                f"carriage moved {moved:.1f}mm in {self._window_s:.1f}s while driven "
                f"(need {self._min_travel_mm:.1f}mm) — check the rail, the limit "
                f"switches and the gearbox before retrying",
            )


class Watchdog:
    """Kills the outputs if the control loop stops ticking.

    Deliberately measured in **wall-clock** time, not simulated time: a wedged
    process is a wall-clock event. In a fast simulation virtual time outruns
    wall time, so this never fires spuriously during tests.
    """

    def __init__(self, board: Board, latch: EStopLatch, timeout_s: float) -> None:
        self._board = board
        self._latch = latch
        self._timeout_s = timeout_s
        self._last_pet = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def pet(self) -> None:
        self._last_pet = time.monotonic()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._last_pet = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="solarsweep-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        poll = min(0.25, self._timeout_s / 4)
        while not self._stop.wait(poll):
            if time.monotonic() - self._last_pet > self._timeout_s:
                logger.critical(
                    "Watchdog expired after %.1fs with no control tick — "
                    "de-energising all outputs", self._timeout_s
                )
                try:
                    self._board.all_stop()
                finally:
                    self._latch.trip(
                        StopReason.WATCHDOG,
                        f"control loop silent for >{self._timeout_s:.1f}s",
                    )
                return


class SafetyMonitor:
    """Every interlock, checked once per control tick.

    ``tick()`` is called from inside every motion loop. It reads the board,
    trips the latch on anything alarming, and raises :class:`SafetyAbort`.
    There is no path through this function that lets a caller decide to keep
    going.
    """

    def __init__(self, board: Board, settings, latch: EStopLatch) -> None:
        self._board = board
        self._cfg = settings.safety
        self._latch = latch
        self._clock = board.clock
        self._overcurrent_since: dict[str, float] = {}
        self.stall = StallDetector(
            board.clock, self._cfg.stall_window_s, self._cfg.stall_min_travel_mm
        )
        self.watchdog = Watchdog(board, latch, self._cfg.watchdog_timeout_s)

    def start(self) -> None:
        self.watchdog.start()

    def stop(self) -> None:
        self.watchdog.stop()

    def preflight(self) -> None:
        """Refuse to start a cycle when conditions already say no."""
        self._latch.raise_if_tripped()
        if self._board.read_input(InputId.ESTOP):
            self._latch.trip(StopReason.ESTOP_BUTTON, "e-stop asserted before start")
            self._latch.raise_if_tripped()
        if self._cfg.rain_blocks_start and self._board.read_input(InputId.RAIN):
            raise SafetyAbort(StopReason.RAIN, "rain detected at start of cycle")
        if self._cfg.obstacle_stops_run and self._board.read_input(InputId.OBSTACLE):
            raise SafetyAbort(StopReason.OBSTACLE, "obstacle present before start")

    def tick(self, position_mm: float | None = None, moving: bool = False) -> None:
        """One safety evaluation. Raises rather than returning a status."""
        self.watchdog.pet()
        self._latch.raise_if_tripped()

        if self._board.read_input(InputId.ESTOP):
            self._latch.trip(StopReason.ESTOP_BUTTON, "e-stop button asserted")
            self._latch.raise_if_tripped()

        if self._cfg.obstacle_stops_run and self._board.read_input(InputId.OBSTACLE):
            raise SafetyAbort(StopReason.OBSTACLE, "obstacle detected on the rail")

        if self._cfg.rain_aborts_run and self._board.read_input(InputId.RAIN):
            raise SafetyAbort(StopReason.RAIN, "rain started mid-run")

        if moving and position_mm is not None:
            self.stall.update(position_mm)

        self._check_overcurrent()

    def _check_overcurrent(self) -> None:
        limit = self._cfg.overcurrent_a
        if limit <= 0:
            return  # no sensing fitted
        from .hal import MotorId

        for motor in (MotorId.DRIVE, MotorId.BRUSH):
            amps = self._board.read_current(motor)
            if amps is None:
                continue
            key = motor.value
            if amps <= limit:
                self._overcurrent_since.pop(key, None)
                continue
            since = self._overcurrent_since.setdefault(key, self._clock.now())
            if self._clock.now() - since >= self._cfg.overcurrent_grace_s:
                raise SafetyAbort(
                    StopReason.OVERCURRENT,
                    f"{key} drew {amps:.2f}A (limit {limit:.2f}A) for "
                    f"{self._cfg.overcurrent_grace_s:.1f}s",
                )
