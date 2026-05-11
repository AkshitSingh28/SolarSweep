"""Configuration loader for Solar Cleaner Robot."""
import yaml
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GPIOConfig:
    motor_drive_in1: int = 17
    motor_drive_in2: int = 27
    motor_drive_en: int = 22
    motor_lift_in1: int = 23
    motor_lift_in2: int = 24
    motor_lift_en: int = 25
    motor_brush_in1: int = 5
    motor_brush_in2: int = 6
    motor_brush_en: int = 13
    relay_water_pump: int = 16
    relay_aux: int = 20
    limit_switch_front: int = 18
    limit_switch_rear: int = 19
    limit_switch_top: int = 21
    limit_switch_bottom: int = 26
    rain_sensor: int = 12
    dust_sensor_adc: int = 0
    ir_obstacle: int = 4


@dataclass
class CleaningConfig:
    passes: int = 3
    brush_speed_pct: int = 80
    water_pulse_ms: int = 500
    water_interval_s: int = 10
    lift_height_mm: int = 30
    overlap_mm: int = 10


@dataclass
class RobotConfig:
    name: str = "SolarCleaner-v1"
    panel_length_mm: int = 2000
    panel_width_mm: int = 1000
    cleaning_speed_mmps: int = 50
    travel_speed_mmps: int = 150


class Settings:
    def __init__(self, config_path: str = "config/settings.yaml"):
        self._raw = {}
        if os.path.exists(config_path):
            with open(config_path) as f:
                self._raw = yaml.safe_load(f) or {}

        self.robot = self._parse_robot()
        self.gpio = self._parse_gpio()
        self.cleaning = self._parse_cleaning()
        self.web_port: int = self._raw.get("web", {}).get("port", 5000)
        self.web_host: str = self._raw.get("web", {}).get("host", "0.0.0.0")
        self.scheduler_enabled: bool = self._raw.get("scheduler", {}).get("enabled", False)
        self.scheduler_jobs: list = self._raw.get("scheduler", {}).get("jobs", [])
        self.dust_threshold: int = self._raw.get("sensors", {}).get("dust_threshold", 200)
        self.rain_disable: bool = self._raw.get("sensors", {}).get("rain_disable", True)

    def _parse_robot(self) -> RobotConfig:
        r = self._raw.get("robot", {})
        return RobotConfig(**{k: v for k, v in r.items() if hasattr(RobotConfig, k)})

    def _parse_gpio(self) -> GPIOConfig:
        g = self._raw.get("hardware", {}).get("gpio", {})
        return GPIOConfig(**{k: v for k, v in g.items() if hasattr(GPIOConfig, k)})

    def _parse_cleaning(self) -> CleaningConfig:
        c = self._raw.get("cleaning", {})
        return CleaningConfig(**{k: v for k, v in c.items() if hasattr(CleaningConfig, k)})
