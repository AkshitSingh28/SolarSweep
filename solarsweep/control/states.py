"""Robot states and the transitions that are legal between them.

v1 had a ten-member state enum and assigned to it freely — nothing checked
that a transition made sense, so ``PAUSED`` was reachable only from
``CLEANING`` while the traverse loop that read it always ran in ``TRAVERSING``.
The pause button was dead code that nobody could have noticed without running
the machine.

Here the table is explicit and ``StateMachine.to()`` rejects anything not in
it, so that class of bug fails immediately and in the test suite.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum

logger = logging.getLogger(__name__)


class RobotState(str, Enum):
    #: Powered, homed or not, doing nothing.
    IDLE = "idle"
    #: Checking interlocks and deciding whether the cycle is worth running.
    PREFLIGHT = "preflight"
    #: Seeking the home end stop.
    HOMING = "homing"
    #: Moving to the start of a pass with the head off.
    POSITIONING = "positioning"
    #: Brush turning, water pulsing, carriage traversing.
    CLEANING = "cleaning"
    #: Operator-requested hold. Motion stopped, cycle not abandoned.
    PAUSED = "paused"
    #: Returning to the park position at the end of a cycle.
    PARKING = "parking"
    #: A safety interlock tripped. Requires an explicit clear.
    FAULTED = "faulted"
    #: E-stop latched. The most restrictive state; only a reset leaves it.
    ESTOPPED = "estopped"


#: state -> states it may move to.
TRANSITIONS: dict[RobotState, frozenset[RobotState]] = {
    RobotState.IDLE: frozenset({
        RobotState.PREFLIGHT, RobotState.HOMING, RobotState.FAULTED,
        RobotState.ESTOPPED,
    }),
    RobotState.PREFLIGHT: frozenset({
        RobotState.HOMING, RobotState.IDLE, RobotState.FAULTED, RobotState.ESTOPPED,
    }),
    RobotState.HOMING: frozenset({
        RobotState.POSITIONING, RobotState.IDLE, RobotState.PAUSED,
        RobotState.FAULTED, RobotState.ESTOPPED,
    }),
    RobotState.POSITIONING: frozenset({
        RobotState.CLEANING, RobotState.PARKING, RobotState.PAUSED,
        RobotState.FAULTED, RobotState.ESTOPPED,
    }),
    RobotState.CLEANING: frozenset({
        RobotState.POSITIONING, RobotState.PARKING, RobotState.PAUSED,
        RobotState.FAULTED, RobotState.ESTOPPED,
    }),
    # Pause returns to whatever was running; the robot remembers where it came
    # from rather than guessing, so resuming mid-pass resumes mid-pass.
    RobotState.PAUSED: frozenset({
        RobotState.HOMING, RobotState.POSITIONING, RobotState.CLEANING,
        RobotState.PARKING, RobotState.FAULTED, RobotState.ESTOPPED,
    }),
    RobotState.PARKING: frozenset({
        RobotState.IDLE, RobotState.PAUSED, RobotState.FAULTED, RobotState.ESTOPPED,
    }),
    # A fault must be acknowledged. It cannot silently become IDLE.
    RobotState.FAULTED: frozenset({RobotState.IDLE, RobotState.ESTOPPED}),
    RobotState.ESTOPPED: frozenset({RobotState.IDLE}),
}

#: States in which the drive motor may be energised.
MOVING_STATES = frozenset({
    RobotState.HOMING, RobotState.POSITIONING, RobotState.CLEANING, RobotState.PARKING
})

#: States a new cycle may be started from.
STARTABLE_STATES = frozenset({RobotState.IDLE})


class IllegalTransition(RuntimeError):
    pass


class PauseGate:
    """Operator hold that motion loops actually observe.

    v1's ``pause()`` set a state that the traverse loop never saw. This is a
    gate the loop passes through on every tick, so a pause takes effect within
    one control period wherever the carriage happens to be.
    """

    def __init__(self) -> None:
        self._paused = threading.Event()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def wait(self, tick, clock) -> float:
        """Block until resumed, still running safety checks. Returns the
        seconds spent paused so callers can extend their deadlines."""
        if not self._paused.is_set():
            return 0.0
        start = clock.now()
        while self._paused.is_set():
            tick()
            clock.sleep(0.05)
        return clock.now() - start


class StateMachine:
    def __init__(self, initial: RobotState = RobotState.IDLE) -> None:
        self._state = initial
        self._previous = initial
        self._lock = threading.Lock()
        self._listeners: list = []

    @property
    def state(self) -> RobotState:
        with self._lock:
            return self._state

    @property
    def previous(self) -> RobotState:
        with self._lock:
            return self._previous

    def add_listener(self, callback) -> None:
        """Called with (old, new) after every accepted transition."""
        self._listeners.append(callback)

    def can(self, target: RobotState) -> bool:
        with self._lock:
            return target in TRANSITIONS[self._state] or target is self._state

    def to(self, target: RobotState) -> None:
        with self._lock:
            current = self._state
            if target is current:
                return
            if target not in TRANSITIONS[current]:
                raise IllegalTransition(
                    f"{current.value} -> {target.value} is not a legal transition"
                )
            self._previous = current
            self._state = target
        logger.info("state: %s -> %s", current.value, target.value)
        for callback in self._listeners:
            try:
                callback(current, target)
            except Exception:  # pragma: no cover - a listener must not break control
                logger.exception("state listener failed")

    def force(self, target: RobotState) -> None:
        """Bypass the table. Only for e-stop, which must be reachable from
        anywhere including states we did not anticipate."""
        with self._lock:
            current = self._state
            if target is current:
                return
            self._previous = current
            self._state = target
        logger.warning("state: %s -> %s (forced)", current.value, target.value)
        for callback in self._listeners:
            try:
                callback(current, target)
            except Exception:  # pragma: no cover
                logger.exception("state listener failed")
