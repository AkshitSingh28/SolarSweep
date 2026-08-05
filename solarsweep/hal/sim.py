"""Kinematic simulator backend.

This is what lets the project be tested. The v1 prototype "could not be
tested" — so every safety path in it (homing timeouts, stall detection,
e-stop) was written blind and never executed once. Here the same control code
runs against a model of the carriage, and faults that would cost a gearbox on
a real roof are just a string on the command line::

    solarsweep run --sim --fault stuck_limit_far
    solarsweep run --sim --fault "rain@30,drive_stall@900"

The model is deliberately simple: a first-order velocity lag, a position
integrator, hard end stops, and switches that assert from position. It is not
trying to be a dynamics package. It is trying to make every branch in
``control/`` reachable.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from ..config import Settings
from .base import Board, BoardInfo, Clock, InputId, MotorId, RelayId

#: Physics integration step. Small enough that a 60 mm/s carriage moves
#: 0.6 mm per step, well under the 4 mm stall threshold.
STEP_DT = 0.01


class SimClock(Clock):
    """Virtual time that the physics is stepped against.

    ``time_scale`` is a speedup: 1.0 runs in real time, 20.0 runs a 30-second
    pass in 1.5 seconds, and 0 runs as fast as the CPU manages (what the test
    suite uses).
    """

    def __init__(self, time_scale: float = 1.0, step_dt: float = STEP_DT) -> None:
        self._t = 0.0
        self._time_scale = time_scale
        self._step_dt = step_dt
        self._listeners: list = []

    def add_listener(self, callback) -> None:
        self._listeners.append(callback)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        """Move virtual time forward, stepping the physics as we go."""
        remaining = seconds
        while remaining > 1e-9:
            dt = min(self._step_dt, remaining)
            self._t += dt
            for callback in self._listeners:
                callback(dt)
            remaining -= dt

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._time_scale > 0:
            time.sleep(seconds / self._time_scale)
        self.advance(seconds)


@dataclass
class FaultSpec:
    """A single injected fault, optionally armed at a threshold.

    Parsed from ``name`` or ``name@value``, where the meaning of ``value``
    depends on the fault (seconds for time-based, mm for position-based).
    """

    name: str
    value: float | None = None

    @staticmethod
    def parse(spec: str) -> list[FaultSpec]:
        out: list[FaultSpec] = []
        for chunk in spec.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "@" in chunk:
                name, _, raw = chunk.partition("@")
                try:
                    value = float(raw)
                except ValueError as exc:
                    raise ValueError(f"fault {chunk!r}: {raw!r} is not a number") from exc
                out.append(FaultSpec(name.strip(), value))
            else:
                out.append(FaultSpec(chunk))
        known = {
            "stuck_limit_home", "stuck_limit_far", "drive_stall", "brush_stall",
            "rain", "obstacle", "estop", "no_water", "overcurrent", "encoder_dead",
        }
        for fault in out:
            if fault.name not in known:
                raise ValueError(
                    f"unknown fault {fault.name!r}. Known: {', '.join(sorted(known))}"
                )
        return out


@dataclass
class CarriageState:
    """Everything the simulated machine knows about itself."""

    position_mm: float = 0.0
    velocity_mmps: float = 0.0
    drive_duty: float = 0.0
    brush_duty: float = 0.0
    brush_rpm: float = 0.0
    pump_on: bool = False
    aux_on: bool = False
    water_dispensed_ml: float = 0.0
    encoder_origin_mm: float = 0.0
    #: Surface dirtiness, 0-1. Cleaning reduces it under the brush.
    soiling: float = 1.0
    distance_travelled_mm: float = 0.0
    stalled: bool = False
    estop_pressed: bool = False
    raining: bool = False
    obstacle_present: bool = False
    water_available: bool = True
    swept_mm: list = field(default_factory=list)


class SimBoard(Board):
    """A carriage on a rail, with switches, a brush, and a pump."""

    #: Time constant of the velocity lag, seconds. Gearmotor + carriage mass.
    TAU_S = 0.25
    #: Pump flow at full duty, ml/s.
    PUMP_ML_PER_S = 9.0
    #: How much soiling one brush pass removes at nominal duty.
    CLEAN_RATE_PER_S = 0.22

    def __init__(self, settings: Settings, seed: int = 0) -> None:
        self._settings = settings
        self._sim = settings.sim
        self._clock = SimClock(self._sim.time_scale)
        self._clock.add_listener(self._step)
        self._state = CarriageState()
        self._rng = random.Random(seed)
        self._faults = FaultSpec.parse(self._sim.fault) if self._sim.fault else []
        self._fault_names = {f.name for f in self._faults}
        self._closed = False
        #: Set when the model detects the carriage jammed against an end stop.
        self._pinned = False

    # -- Introspection used by tests and the dashboard --------------------

    @property
    def state(self) -> CarriageState:
        return self._state

    @property
    def info(self) -> BoardInfo:
        return BoardInfo(
            backend="sim",
            simulated=True,
            has_encoder=self._settings.drive.closed_loop,
            has_current_sense=self._settings.safety.overcurrent_a > 0,
        )

    @property
    def clock(self) -> Clock:
        return self._clock

    def _fault(self, name: str) -> FaultSpec | None:
        for f in self._faults:
            if f.name == name:
                return f
        return None

    def _fault_armed(self, name: str, current: float) -> bool:
        """True when a fault exists and its threshold has been crossed."""
        fault = self._fault(name)
        if fault is None:
            return False
        return fault.value is None or current >= fault.value

    # -- Physics ----------------------------------------------------------

    def _step(self, dt: float) -> None:
        s = self._state
        drive = self._settings.drive
        panel = self._settings.panel

        # Commanded speed. Below min_duty the gearmotor does not overcome its
        # own stiction, which is why the config carries that number.
        duty = s.drive_duty
        if abs(duty) < drive.min_duty_pct:
            target = 0.0
        else:
            target = (duty / 100.0) * drive.duty_to_mmps * self._sim.efficiency

        if s.stalled or self._pinned:
            target = 0.0

        # First-order lag toward the target.
        alpha = min(1.0, dt / self.TAU_S)
        s.velocity_mmps += (target - s.velocity_mmps) * alpha

        previous = s.position_mm
        s.position_mm += s.velocity_mmps * dt

        # Hard end stops. The carriage physically cannot leave the rail; if a
        # limit switch is stuck the software will keep driving into this and
        # the stall detector is the only thing left to save the gearbox.
        if s.position_mm <= 0.0:
            s.position_mm = 0.0
            self._pinned = abs(duty) >= drive.min_duty_pct and duty < 0
            if self._pinned:
                s.velocity_mmps = 0.0
        elif s.position_mm >= panel.rail_travel_mm:
            s.position_mm = panel.rail_travel_mm
            self._pinned = abs(duty) >= drive.min_duty_pct and duty > 0
            if self._pinned:
                s.velocity_mmps = 0.0
        else:
            self._pinned = False

        s.distance_travelled_mm += abs(s.position_mm - previous)

        # Injected mechanical stall, armed on cumulative distance.
        if self._fault_armed("drive_stall", s.distance_travelled_mm):
            s.stalled = True
            s.velocity_mmps = 0.0

        # Brush spins up toward its commanded duty.
        brush_target = abs(s.brush_duty) * 18.0  # ~1800 rpm at full duty
        if self._fault_armed("brush_stall", self._clock.now()):
            brush_target = 0.0
        s.brush_rpm += (brush_target - s.brush_rpm) * min(1.0, dt / 0.4)

        # Water and cleaning effect.
        if s.pump_on and s.water_available:
            s.water_dispensed_ml += self.PUMP_ML_PER_S * dt
        if s.brush_rpm > 200 and abs(s.velocity_mmps) > 1.0:
            wet_bonus = 1.0 if (s.pump_on and s.water_available) else 0.55
            s.soiling = max(
                0.0, s.soiling - self.CLEAN_RATE_PER_S * dt * wet_bonus
            )
            s.swept_mm.append(round(s.position_mm, 1))

        # Time- and position-armed environmental faults.
        now = self._clock.now()
        if self._fault_armed("rain", now):
            s.raining = True
        if self._fault_armed("obstacle", s.position_mm):
            s.obstacle_present = True
        if self._fault_armed("estop", now):
            s.estop_pressed = True
        if "no_water" in self._fault_names:
            s.water_available = False

    # -- Board interface --------------------------------------------------

    def set_motor(self, motor: MotorId, duty_pct: float) -> None:
        self._require_open()
        duty = max(-100.0, min(100.0, float(duty_pct)))
        if motor is MotorId.DRIVE:
            self._state.drive_duty = duty
            if duty == 0.0:
                self._pinned = False
        else:
            self._state.brush_duty = duty

    def brake_motor(self, motor: MotorId) -> None:
        self._require_open()
        if motor is MotorId.DRIVE:
            self._state.drive_duty = 0.0
            self._state.velocity_mmps = 0.0
            self._pinned = False
        else:
            self._state.brush_duty = 0.0
            self._state.brush_rpm = 0.0

    def set_relay(self, relay: RelayId, on: bool) -> None:
        self._require_open()
        if relay is RelayId.PUMP:
            self._state.pump_on = on
        else:
            self._state.aux_on = on

    def read_input(self, channel: InputId) -> bool:
        s = self._state
        panel = self._settings.panel
        if channel is InputId.LIMIT_HOME:
            if "stuck_limit_home" in self._fault_names:
                return False
            return s.position_mm <= 1.5
        if channel is InputId.LIMIT_FAR:
            if "stuck_limit_far" in self._fault_names:
                return False
            return s.position_mm >= panel.rail_travel_mm - 1.5
        if channel is InputId.ESTOP:
            return s.estop_pressed
        if channel is InputId.RAIN:
            return s.raining
        if channel is InputId.OBSTACLE:
            return s.obstacle_present
        if channel is InputId.WATER_PRESSURE:
            return s.water_available
        raise ValueError(f"unhandled input {channel}")

    def read_dust(self) -> int:
        base = self._sim.dust_baseline * self._state.soiling
        noise = self._rng.gauss(0, self._sim.dust_noise)
        return max(0, min(1023, int(base + noise)))

    def read_current(self, motor: MotorId) -> float | None:
        if self._settings.safety.overcurrent_a <= 0:
            return None
        s = self._state
        duty = abs(s.drive_duty if motor is MotorId.DRIVE else s.brush_duty)
        # Free-running draw scales with duty; a stalled or pinned motor pulls
        # locked-rotor current, which is what the overcurrent trip is for.
        stalled = (motor is MotorId.DRIVE) and (s.stalled or self._pinned)
        if self._fault_armed("overcurrent", self._clock.now()):
            stalled = True
        if duty == 0:
            return 0.0
        amps = 0.35 + duty * 0.014
        if stalled:
            amps = 0.35 + duty * 0.075
        return round(amps, 3)

    def read_encoder_mm(self) -> float | None:
        if not self._settings.drive.closed_loop:
            return None
        if "encoder_dead" in self._fault_names:
            return 0.0
        return self._state.position_mm - self._state.encoder_origin_mm

    def reset_encoder(self) -> None:
        self._state.encoder_origin_mm = self._state.position_mm

    def all_stop(self) -> None:
        s = self._state
        s.drive_duty = 0.0
        s.brush_duty = 0.0
        s.velocity_mmps = 0.0
        s.brush_rpm = 0.0
        s.pump_on = False
        s.aux_on = False
        self._pinned = False

    def close(self) -> None:
        self.all_stop()
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("board is closed")

    # -- Test helpers ------------------------------------------------------

    def place_carriage(self, position_mm: float) -> None:
        """Teleport the carriage. Used by tests to set up a scenario."""
        self._state.position_mm = max(
            0.0, min(self._settings.panel.rail_travel_mm, position_mm)
        )
        self._state.velocity_mmps = 0.0

    def press_estop(self) -> None:
        self._state.estop_pressed = True

    def release_estop(self) -> None:
        self._state.estop_pressed = False

    def clear_stall(self) -> None:
        self._state.stalled = False
        self._faults = [f for f in self._faults if f.name != "drive_stall"]
        self._fault_names = {f.name for f in self._faults}

    @property
    def coverage_pct(self) -> float:
        """Fraction of the cleanable span the brush actually passed over.

        This is the number that says whether a cycle did its job, and it is
        the reason the simulator tracks swept positions at all.
        """
        span_start = self._settings.panel.lead_in_mm
        span_end = self._settings.panel.rail_travel_mm - self._settings.panel.lead_out_mm
        if span_end <= span_start:
            return 0.0
        bucket_mm = 10.0
        total = int((span_end - span_start) / bucket_mm) or 1
        hit = {
            int((p - span_start) / bucket_mm)
            for p in self._state.swept_mm
            if span_start <= p <= span_end
        }
        return min(100.0, 100.0 * len(hit) / total)
