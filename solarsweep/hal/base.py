"""Hardware abstraction layer.

The control code above this line never touches a GPIO pin. It talks to a
``Board``, which is either the real Raspberry Pi (``hal.rpi``) or a kinematic
simulator (``hal.sim``). That split is the whole reason v2 can be tested: the
v1 prototype was never run, and its control logic could not be exercised
without physically standing on a roof next to it.

Two conventions matter throughout:

* **Signed duty.** Motors take a single number in ``[-100, 100]``. Direction is
  the sign. v1 had separate ``forward()``/``backward()``/``set_speed()`` calls
  that could disagree with each other.
* **Logical inputs.** ``DigitalInput.read()`` returns True when the thing is
  *asserted* (switch pressed, rain falling), regardless of whether the wiring
  is active-high or active-low. Polarity lives in the backend, not in the
  state machine.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class MotorId(str, Enum):
    DRIVE = "drive"
    BRUSH = "brush"


class RelayId(str, Enum):
    PUMP = "pump"
    AUX = "aux"


class InputId(str, Enum):
    LIMIT_HOME = "limit_home"
    LIMIT_FAR = "limit_far"
    ESTOP = "estop"
    RAIN = "rain"
    OBSTACLE = "obstacle"
    WATER_PRESSURE = "water_pressure"


class HardwareError(RuntimeError):
    """A backend could not do what was asked of it."""


@dataclass(frozen=True)
class BoardInfo:
    backend: str
    simulated: bool
    has_encoder: bool
    has_current_sense: bool


class Clock(ABC):
    """Time, injectable.

    Tests and the simulator need to advance time without waiting for it. Every
    timeout in the control layer is measured against a Clock, never against
    ``time.time()`` directly.
    """

    @abstractmethod
    def now(self) -> float:
        """Monotonic seconds. Only differences are meaningful."""

    @abstractmethod
    def sleep(self, seconds: float) -> None:
        ...


class RealClock(Clock):
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class Board(ABC):
    """One physical (or simulated) machine."""

    @property
    @abstractmethod
    def info(self) -> BoardInfo:
        ...

    @property
    @abstractmethod
    def clock(self) -> Clock:
        ...

    # -- Actuators ---------------------------------------------------------

    @abstractmethod
    def set_motor(self, motor: MotorId, duty_pct: float) -> None:
        """Drive a motor at a signed duty in [-100, 100]. 0 coasts."""

    @abstractmethod
    def brake_motor(self, motor: MotorId) -> None:
        """Short the motor terminals. Stops faster than coasting, and holds
        the carriage against gravity on a tilted panel."""

    @abstractmethod
    def set_relay(self, relay: RelayId, on: bool) -> None:
        ...

    # -- Sensors -----------------------------------------------------------

    @abstractmethod
    def read_input(self, channel: InputId) -> bool:
        """True when asserted. Polarity is handled by the backend."""

    @abstractmethod
    def read_dust(self) -> int:
        """Raw ADC counts, 0-1023. Higher means dirtier."""

    def read_current(self, motor: MotorId) -> float | None:
        """Motor current in amps, or None when no sensing is fitted."""
        return None

    def read_encoder_mm(self) -> float | None:
        """Carriage displacement from the last ``reset_encoder`` in mm, or
        None when no encoder is fitted (then the control layer falls back to
        timed motion)."""
        return None

    def reset_encoder(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Zero the encoder origin. A no-op on boards without one."""

    # -- Lifecycle ---------------------------------------------------------

    @abstractmethod
    def all_stop(self) -> None:
        """De-energise every output. Must be safe to call at any time, from
        any state, including twice in a row and during shutdown."""

    def close(self) -> None:
        self.all_stop()

    def __enter__(self) -> Board:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
