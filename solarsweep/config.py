"""Configuration loading and validation.

Every field defined here is read by something at runtime. If you add a field,
add the code that uses it in the same change — v1 accumulated a settings.yaml
where half the keys were decoration (see docs/V1_AUDIT.md).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config/settings.yaml")


class ConfigError(ValueError):
    """Raised when settings.yaml is malformed or internally inconsistent."""


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PanelConfig:
    """Geometry of the panel array the carriage travels over."""

    #: Usable rail travel, home (0) to far end. Measured, not the panel spec.
    rail_travel_mm: float = 1650.0
    #: Cleaning width covered in a single pass by brush + mop.
    swath_width_mm: float = 350.0
    #: Distance from the home limit switch to where glass actually starts.
    lead_in_mm: float = 60.0
    #: Distance from the far limit switch back to where glass ends.
    lead_out_mm: float = 60.0

    def validate(self) -> None:
        if self.rail_travel_mm <= 0:
            raise ConfigError("panel.rail_travel_mm must be > 0")
        if self.lead_in_mm + self.lead_out_mm >= self.rail_travel_mm:
            raise ConfigError(
                "panel.lead_in_mm + lead_out_mm must be less than rail_travel_mm"
            )
        if self.swath_width_mm <= 0:
            raise ConfigError("panel.swath_width_mm must be > 0")


@dataclass(frozen=True)
class DriveConfig:
    """Rail drive characterisation.

    ``duty_to_mmps`` is the number that makes open-loop motion honest: it is
    measured during ``solarsweep calibrate``, not guessed. v1 divided a
    distance by a PWM duty cycle and called the result a time.
    """

    #: Measured carriage speed at 100% duty, unloaded, mm/s.
    duty_to_mmps: float = 62.0
    #: Duty below which the gearmotor stalls instead of turning, percent.
    min_duty_pct: int = 25
    cleaning_duty_pct: int = 45
    transit_duty_pct: int = 75
    homing_duty_pct: int = 35
    #: Seconds to ramp between duty levels. Protects gearbox and rail wheels.
    ramp_time_s: float = 0.6
    #: Encoder ticks per mm of carriage travel. 0 disables closed-loop motion
    #: and falls back to timed motion using duty_to_mmps.
    encoder_ticks_per_mm: float = 0.0

    def validate(self) -> None:
        if self.duty_to_mmps <= 0:
            raise ConfigError("drive.duty_to_mmps must be > 0 (run: solarsweep calibrate)")
        for name in ("min_duty_pct", "cleaning_duty_pct", "transit_duty_pct",
                     "homing_duty_pct"):
            value = getattr(self, name)
            if not 0 < value <= 100:
                raise ConfigError(f"drive.{name} must be in (0, 100]")
        for name in ("cleaning_duty_pct", "transit_duty_pct", "homing_duty_pct"):
            if getattr(self, name) < self.min_duty_pct:
                raise ConfigError(
                    f"drive.{name} is below drive.min_duty_pct — the motor would stall"
                )
        if self.ramp_time_s < 0:
            raise ConfigError("drive.ramp_time_s must be >= 0")
        if self.encoder_ticks_per_mm < 0:
            raise ConfigError("drive.encoder_ticks_per_mm must be >= 0")

    @property
    def closed_loop(self) -> bool:
        return self.encoder_ticks_per_mm > 0


@dataclass(frozen=True)
class CleaningConfig:
    passes: int = 2
    brush_duty_pct: int = 70
    #: Seconds the brush is given to spin up before the carriage starts moving.
    brush_spinup_s: float = 1.5
    #: Water is pulsed, not run continuously — a tethered supply on a rooftop
    #: panel is a limited resource and standing water streaks as it dries.
    water_pulse_ms: int = 600
    water_interval_s: float = 8.0
    #: Skip water entirely (dry dusting pass). Overridden to True when the
    #: supply pressure switch reports no water.
    dry_pass: bool = False

    def validate(self) -> None:
        if self.passes < 1:
            raise ConfigError("cleaning.passes must be >= 1")
        if not 0 < self.brush_duty_pct <= 100:
            raise ConfigError("cleaning.brush_duty_pct must be in (0, 100]")
        if self.water_pulse_ms < 0:
            raise ConfigError("cleaning.water_pulse_ms must be >= 0")
        if self.water_interval_s <= 0:
            raise ConfigError("cleaning.water_interval_s must be > 0")
        if self.water_pulse_ms / 1000.0 >= self.water_interval_s:
            raise ConfigError(
                "cleaning.water_pulse_ms must be shorter than water_interval_s, "
                "otherwise the pump never rests"
            )


@dataclass(frozen=True)
class SafetyConfig:
    """Every bound that stops the machine hurting itself.

    v1 had none of these: homing spun forever against a dead limit switch and
    an e-stop was a log line the caller ignored.
    """

    #: Hard ceiling on any homing move. Exceeded => fault, motors off.
    home_timeout_s: float = 90.0
    #: Hard ceiling on a single traverse, as a multiple of the predicted time.
    traverse_timeout_factor: float = 1.8
    #: Absolute ceiling regardless of factor.
    traverse_timeout_max_s: float = 300.0
    #: If commanded to move and the encoder/position has not changed by
    #: stall_min_travel_mm within this window, treat it as a stall.
    stall_window_s: float = 2.5
    stall_min_travel_mm: float = 4.0
    #: Motor current above this for longer than overcurrent_grace_s => fault.
    #: Set to 0 if no current sensing is fitted (then it is never checked).
    overcurrent_a: float = 0.0
    overcurrent_grace_s: float = 1.0
    #: Control loop must tick at least this often or the watchdog fires.
    watchdog_timeout_s: float = 3.0
    #: Refuse to start a cycle when rain is detected.
    rain_blocks_start: bool = True
    #: Abort an in-progress cycle if rain starts mid-run.
    rain_aborts_run: bool = True
    #: Stop on the obstacle sensor.
    obstacle_stops_run: bool = True
    #: Consecutive faults before the robot latches out and stops retrying.
    max_consecutive_faults: int = 3
    #: On the first cycle after startup, drive the full rail to confirm both
    #: limit switches work and to measure the true travel. Costs one traverse
    #: and catches a dead far switch, which a position-targeted pass never
    #: touches — see docs/V1_AUDIT.md.
    reference_on_start: bool = True
    #: How far measured rail travel may differ from panel.rail_travel_mm
    #: before the referencing run calls it a fault.
    rail_travel_tolerance_pct: float = 10.0

    def validate(self) -> None:
        for name in ("home_timeout_s", "traverse_timeout_max_s", "stall_window_s",
                     "watchdog_timeout_s"):
            if getattr(self, name) <= 0:
                raise ConfigError(f"safety.{name} must be > 0")
        if self.traverse_timeout_factor <= 1.0:
            raise ConfigError("safety.traverse_timeout_factor must be > 1.0")
        if self.stall_min_travel_mm <= 0:
            raise ConfigError("safety.stall_min_travel_mm must be > 0")
        if self.overcurrent_a < 0:
            raise ConfigError("safety.overcurrent_a must be >= 0 (0 disables the check)")
        if self.max_consecutive_faults < 1:
            raise ConfigError("safety.max_consecutive_faults must be >= 1")
        if not 0 < self.rail_travel_tolerance_pct <= 100:
            raise ConfigError("safety.rail_travel_tolerance_pct must be in (0, 100]")


@dataclass(frozen=True)
class PinConfig:
    """BCM pin numbers. See docs/WIRING.md for the physical map."""

    drive_in1: int = 17
    drive_in2: int = 27
    drive_en: int = 22
    brush_in1: int = 5
    brush_in2: int = 6
    brush_en: int = 13
    relay_pump: int = 16
    relay_aux: int = 20
    limit_home: int = 19
    limit_far: int = 18
    estop_button: int = 4
    rain_sensor: int = 12
    obstacle_sensor: int = 23
    water_pressure: int = 24
    #: Optional quadrature encoder on the drive wheel. -1 = not fitted.
    encoder_a: int = -1
    encoder_b: int = -1

    #: Inputs wired normally-closed read HIGH when *undisturbed*. Limit
    #: switches and the e-stop should be NC so a cut wire reads as triggered.
    limits_normally_closed: bool = True
    estop_normally_closed: bool = True
    #: Most cheap relay boards are active-LOW.
    relays_active_low: bool = True

    def validate(self) -> None:
        assigned: dict[int, str] = {}
        for f in fields(self):
            if f.type != "int":
                continue
            value = getattr(self, f.name)
            if value < 0:
                continue  # -1 means "not fitted"
            if not 0 <= value <= 27:
                raise ConfigError(f"pins.{f.name}={value} is not a valid BCM pin (0-27)")
            if value in assigned:
                raise ConfigError(
                    f"pins.{f.name} and pins.{assigned[value]} are both on BCM {value}"
                )
            assigned[value] = f.name


@dataclass(frozen=True)
class TriggerConfig:
    """When the robot decides a clean is worth doing."""

    #: Skip the cycle if the dust reading is below this. Set to 0 to always run.
    dust_threshold: int = 0
    #: Minimum hours between cycles, regardless of what the schedule says.
    min_hours_between_runs: float = 6.0

    def validate(self) -> None:
        if self.dust_threshold < 0:
            raise ConfigError("triggers.dust_threshold must be >= 0")
        if self.min_hours_between_runs < 0:
            raise ConfigError("triggers.min_hours_between_runs must be >= 0")


@dataclass(frozen=True)
class ScheduleEntry:
    cron: str
    mode: str = "full_cycle"

    def validate(self) -> None:
        parts = self.cron.split()
        if len(parts) != 5:
            raise ConfigError(
                f"schedule cron {self.cron!r} must have 5 fields "
                "(minute hour day month day_of_week)"
            )
        if self.mode not in ("full_cycle", "quick_pass"):
            raise ConfigError(f"schedule mode {self.mode!r} must be full_cycle or quick_pass")


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool = False
    timezone: str = "Asia/Kolkata"
    jobs: tuple[ScheduleEntry, ...] = ()

    def validate(self) -> None:
        for job in self.jobs:
            job.validate()


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 5000
    #: Read from SOLARSWEEP_WEB_TOKEN. When set, mutating endpoints require it.
    #: Left empty the dashboard is read-only unless bound to loopback.
    auth_token: str = ""

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError("web.port must be 1-65535")
        if self.host not in ("127.0.0.1", "localhost") and not self.auth_token:
            raise ConfigError(
                "web.host is not loopback but no auth token is set. Export "
                "SOLARSWEEP_WEB_TOKEN=<secret> or bind to 127.0.0.1."
            )


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/solarsweep.log"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3
    #: Machine-readable per-run record, one JSON object per line.
    telemetry_dir: str = "logs/runs"

    def validate(self) -> None:
        if self.level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(f"logging.level {self.level!r} is not a valid level")


@dataclass(frozen=True)
class SimConfig:
    """Simulator behaviour. Ignored entirely on real hardware."""

    #: Wall-clock speedup. 0 means "as fast as the CPU allows" (used by tests).
    time_scale: float = 1.0
    #: Fraction of commanded speed actually achieved. Models rail friction.
    efficiency: float = 0.92
    #: Sensor noise on the dust ADC reading, in counts.
    dust_noise: int = 12
    dust_baseline: int = 260
    #: Fault injection, driven by tests and by `solarsweep run --sim --fault=`.
    fault: str = ""

    def validate(self) -> None:
        if self.time_scale < 0:
            raise ConfigError("sim.time_scale must be >= 0")
        if not 0 < self.efficiency <= 1.0:
            raise ConfigError("sim.efficiency must be in (0, 1]")


@dataclass(frozen=True)
class Settings:
    name: str = "SolarSweep-v2"
    panel: PanelConfig = field(default_factory=PanelConfig)
    drive: DriveConfig = field(default_factory=DriveConfig)
    cleaning: CleaningConfig = field(default_factory=CleaningConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    pins: PinConfig = field(default_factory=PinConfig)
    triggers: TriggerConfig = field(default_factory=TriggerConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    web: WebConfig = field(default_factory=WebConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    sim: SimConfig = field(default_factory=SimConfig)

    def validate(self) -> None:
        for f in fields(self):
            section = getattr(self, f.name)
            if is_dataclass(section) and hasattr(section, "validate"):
                section.validate()
        # Cross-section checks: things that are individually fine but
        # nonsensical together.
        cleaning_mmps = self.drive.duty_to_mmps * self.drive.cleaning_duty_pct / 100.0
        pass_time_s = self.panel.rail_travel_mm / cleaning_mmps
        if pass_time_s > self.safety.traverse_timeout_max_s:
            raise ConfigError(
                f"a cleaning pass needs ~{pass_time_s:.0f}s but "
                f"safety.traverse_timeout_max_s is {self.safety.traverse_timeout_max_s}s — "
                "the run would time out before finishing"
            )
        # Homing runs slowly on purpose (you approach a hard stop gently), so
        # its timeout has to allow for the worst case: seeking home from the
        # far end of the rail.
        homing_mmps = self.drive.duty_to_mmps * self.drive.homing_duty_pct / 100.0
        worst_home_s = self.panel.rail_travel_mm / homing_mmps
        if worst_home_s > self.safety.home_timeout_s:
            raise ConfigError(
                f"homing from the far end of the rail needs ~{worst_home_s:.0f}s at "
                f"{self.drive.homing_duty_pct}% duty, but safety.home_timeout_s is "
                f"{self.safety.home_timeout_s}s. Raise the timeout to at least "
                f"{worst_home_s * 1.3:.0f}s, or raise drive.homing_duty_pct."
            )

    @property
    def cleaning_span_mm(self) -> float:
        """Distance actually swept, excluding the lead-in and lead-out."""
        return self.panel.rail_travel_mm - self.panel.lead_in_mm - self.panel.lead_out_mm


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_SECTIONS: dict[str, type] = {
    "panel": PanelConfig,
    "drive": DriveConfig,
    "cleaning": CleaningConfig,
    "safety": SafetyConfig,
    "pins": PinConfig,
    "triggers": TriggerConfig,
    "web": WebConfig,
    "logging": LoggingConfig,
    "sim": SimConfig,
}


def _build_section(name: str, cls: type, raw: Mapping[str, Any]) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(
            f"unknown key(s) in section {name!r}: {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(known))}"
        )
    return cls(**dict(raw))


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Read settings.yaml, apply env overrides, and validate.

    A missing file is fine — the dataclass defaults describe a working
    simulated machine. A *malformed* file is not, and raises ConfigError
    rather than silently falling back, which is how v1 hid its mistakes.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open() as fh:
            loaded = yaml.safe_load(fh)
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")
        raw = loaded

    unknown_sections = set(raw) - set(_SECTIONS) - {"name", "scheduler"}
    if unknown_sections:
        raise ConfigError(
            f"unknown top-level section(s): {', '.join(sorted(unknown_sections))}"
        )

    kwargs: dict[str, Any] = {"name": raw.get("name", "SolarSweep-v2")}
    for key, cls in _SECTIONS.items():
        kwargs[key] = _build_section(key, cls, raw.get(key) or {})

    sched_raw = raw.get("scheduler") or {}
    jobs = tuple(
        ScheduleEntry(cron=str(j.get("cron", "")), mode=str(j.get("mode", "full_cycle")))
        for j in sched_raw.get("jobs", [])
    )
    kwargs["scheduler"] = SchedulerConfig(
        enabled=bool(sched_raw.get("enabled", False)),
        timezone=str(sched_raw.get("timezone", "Asia/Kolkata")),
        jobs=jobs,
    )

    settings = _apply_env_overrides(Settings(**kwargs))
    settings.validate()
    return settings


def _apply_env_overrides(settings: Settings) -> Settings:
    """Secrets come from the environment, never from a file in git."""
    token = os.environ.get("SOLARSWEEP_WEB_TOKEN", "").strip()
    if token:
        from dataclasses import replace

        settings = replace(settings, web=replace(settings.web, auth_token=token))
    return settings
