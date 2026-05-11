"""
SolarCleanerRobot - Main orchestration class.

Coordinates the rail-guided chassis (forward/backward on outer frame),
vertical lift (up/down on steel rods via linear bearings),
and rotating cleaning head (brush + mop).
"""

import time
import logging
from enum import Enum, auto
from typing import Optional

from src.hardware.motors import DriveMotor, LiftMotor, BrushMotor
from src.hardware.relay import RelayController
from src.hardware.sensors import SensorHub
from src.hardware.gpio_manager import GPIOManager
from config import Settings

logger = logging.getLogger(__name__)


class RobotState(Enum):
    IDLE = auto()
    INITIALIZING = auto()
    LIFTING = auto()
    TRAVERSING = auto()
    CLEANING = auto()
    LOWERING = auto()
    HOMING = auto()
    PAUSED = auto()
    ERROR = auto()
    EMERGENCY_STOP = auto()


class SolarCleanerRobot:
    """
    Rail-guided solar panel cleaning robot.

    Motion model:
      - Outer rectangular frame moves FORWARD / BACKWARD along panel length (X-axis)
      - Inner chassis moves UP / DOWN along two steel rods via linear bearings (Z-axis)
      - Center rotating head (brush + mop) provides cleaning action
      - Spring suspension absorbs panel surface variation
    """

    def __init__(self, settings: Settings, dry_run: bool = False):
        self.settings = settings
        self.dry_run = dry_run
        self.state = RobotState.IDLE
        self._position_x_mm: float = 0.0   # along rail (0 = home/rear)
        self._position_z_mm: float = 0.0   # vertical (0 = lowest)
        self._cleaning_pass: int = 0

        if not dry_run:
            self.gpio = GPIOManager(settings.gpio)
            self.drive_motor = DriveMotor(settings.gpio, self.gpio)
            self.lift_motor = LiftMotor(settings.gpio, self.gpio)
            self.brush_motor = BrushMotor(settings.gpio, self.gpio)
            self.relay = RelayController(settings.gpio, self.gpio)
            self.sensors = SensorHub(settings.gpio, self.gpio)
        else:
            logger.info("[DRY RUN] Hardware drivers skipped.")
            self.gpio = None
            self.drive_motor = None
            self.lift_motor = None
            self.brush_motor = None
            self.relay = None
            self.sensors = None

        logger.info(f"Robot '{settings.robot.name}' initialized. dry_run={dry_run}")

    # ------------------------------------------------------------------
    # High-level cycle control
    # ------------------------------------------------------------------

    def initialize(self):
        """Home all axes and verify sensors."""
        logger.info("Initializing robot — homing all axes...")
        self._set_state(RobotState.INITIALIZING)
        self._home_x()
        self._home_z()
        self._set_state(RobotState.IDLE)
        logger.info("Initialization complete. Robot is at home position.")

    def run_full_cycle(self):
        """Execute complete cleaning cycle across the full panel."""
        self.initialize()

        if self._check_rain():
            logger.warning("Rain detected — aborting cleaning cycle.")
            return

        dust_level = self._read_dust()
        logger.info(f"Dust sensor reading: {dust_level} (threshold: {self.settings.dust_threshold})")

        total_passes = self.settings.cleaning.passes
        panel_length = self.settings.robot.panel_length_mm

        logger.info(f"Starting {total_passes}-pass cleaning cycle over {panel_length}mm panel.")

        for pass_num in range(1, total_passes + 1):
            logger.info(f"--- Pass {pass_num}/{total_passes} ---")
            self._cleaning_pass = pass_num

            # Lift chassis to cleaning height
            self._lift_to_clean()

            # Start brush rotation and water
            self._start_cleaning_head()

            # Drive forward across full panel length
            direction = "forward" if pass_num % 2 != 0 else "backward"
            self._traverse(direction, panel_length)

            # Stop cleaning head
            self._stop_cleaning_head()

            # Lower chassis
            self._lower_chassis()

        # Return home
        self._home_x()
        self._set_state(RobotState.IDLE)
        logger.info("Full cleaning cycle complete.")

    def run_quick_pass(self):
        """Single pass — for evening/light dust cycles."""
        self.initialize()
        self._lift_to_clean()
        self._start_cleaning_head()
        self._traverse("forward", self.settings.robot.panel_length_mm)
        self._stop_cleaning_head()
        self._lower_chassis()
        self._home_x()
        self._set_state(RobotState.IDLE)

    def emergency_stop(self):
        """Immediately cut all motor power."""
        logger.critical("EMERGENCY STOP triggered!")
        self._set_state(RobotState.EMERGENCY_STOP)
        if not self.dry_run:
            self.drive_motor.stop()
            self.lift_motor.stop()
            self.brush_motor.stop()
            self.relay.all_off()

    def pause(self):
        if self.state == RobotState.CLEANING:
            self._set_state(RobotState.PAUSED)
            if not self.dry_run:
                self.drive_motor.stop()
                self.brush_motor.stop()

    def resume(self):
        if self.state == RobotState.PAUSED:
            self._set_state(RobotState.CLEANING)

    # ------------------------------------------------------------------
    # Motion primitives
    # ------------------------------------------------------------------

    def _home_x(self):
        """Drive backward until rear limit switch triggers."""
        logger.info("Homing X-axis (rail) ...")
        self._set_state(RobotState.HOMING)
        if self.dry_run:
            self._position_x_mm = 0.0
            time.sleep(0.5)
            return
        self.drive_motor.set_speed(40)
        self.drive_motor.backward()
        while not self.sensors.limit_rear():
            time.sleep(0.05)
            if self._check_obstacle():
                self.emergency_stop()
                return
        self.drive_motor.stop()
        self._position_x_mm = 0.0
        logger.info("X-axis homed.")

    def _home_z(self):
        """Lower chassis until bottom limit switch triggers."""
        logger.info("Homing Z-axis (lift) ...")
        if self.dry_run:
            self._position_z_mm = 0.0
            time.sleep(0.3)
            return
        self.lift_motor.set_speed(50)
        self.lift_motor.down()
        while not self.sensors.limit_bottom():
            time.sleep(0.05)
        self.lift_motor.stop()
        self._position_z_mm = 0.0
        logger.info("Z-axis homed.")

    def _lift_to_clean(self):
        """Raise chassis to cleaning contact height."""
        target = self.settings.cleaning.lift_height_mm
        logger.info(f"Lifting chassis to {target}mm ...")
        self._set_state(RobotState.LIFTING)
        if self.dry_run:
            self._position_z_mm = target
            time.sleep(0.5)
            return
        self.lift_motor.set_speed(60)
        self.lift_motor.up()
        while not self.sensors.limit_top():
            time.sleep(0.05)
        self.lift_motor.stop()
        self._position_z_mm = target

    def _lower_chassis(self):
        """Lower chassis after a pass."""
        logger.info("Lowering chassis ...")
        self._set_state(RobotState.LOWERING)
        if self.dry_run:
            self._position_z_mm = 0.0
            time.sleep(0.3)
            return
        self.lift_motor.set_speed(50)
        self.lift_motor.down()
        while not self.sensors.limit_bottom():
            time.sleep(0.05)
        self.lift_motor.stop()
        self._position_z_mm = 0.0

    def _traverse(self, direction: str, distance_mm: float):
        """Move chassis along rail for given distance."""
        speed = self.settings.robot.cleaning_speed_mmps
        duration = distance_mm / speed
        logger.info(f"Traversing {direction} {distance_mm}mm (~{duration:.1f}s) ...")
        self._set_state(RobotState.TRAVERSING)

        if self.dry_run:
            elapsed = 0.0
            while elapsed < duration:
                if self.state == RobotState.PAUSED:
                    time.sleep(0.1)
                    continue
                time.sleep(0.1)
                elapsed += 0.1
            self._position_x_mm = distance_mm if direction == "forward" else 0.0
            return

        self.drive_motor.set_speed(int(speed * 100 / self.settings.robot.travel_speed_mmps))
        if direction == "forward":
            self.drive_motor.forward()
            limit_check = self.sensors.limit_front
        else:
            self.drive_motor.backward()
            limit_check = self.sensors.limit_rear

        start = time.time()
        while (time.time() - start) < duration:
            if self.state == RobotState.PAUSED:
                self.drive_motor.stop()
                while self.state == RobotState.PAUSED:
                    time.sleep(0.1)
                self.drive_motor.forward() if direction == "forward" else self.drive_motor.backward()

            if limit_check():
                logger.info("Limit switch hit — stopping traverse early.")
                break
            if self._check_obstacle():
                self.emergency_stop()
                return
            time.sleep(0.05)

        self.drive_motor.stop()

    def _start_cleaning_head(self):
        """Start brush rotation and water pump."""
        speed = self.settings.cleaning.brush_speed_pct
        logger.info(f"Starting cleaning head at {speed}% speed.")
        self._set_state(RobotState.CLEANING)
        if not self.dry_run:
            self.brush_motor.set_speed(speed)
            self.brush_motor.spin()
            self.relay.water_pump_on()

    def _stop_cleaning_head(self):
        """Stop brush and water pump."""
        logger.info("Stopping cleaning head.")
        if not self.dry_run:
            self.brush_motor.stop()
            self.relay.water_pump_off()

    # ------------------------------------------------------------------
    # Sensor helpers
    # ------------------------------------------------------------------

    def _check_rain(self) -> bool:
        if self.dry_run or not self.settings.rain_disable:
            return False
        return self.sensors.rain_detected()

    def _check_obstacle(self) -> bool:
        if self.dry_run:
            return False
        return self.sensors.obstacle_detected()

    def _read_dust(self) -> int:
        if self.dry_run:
            return 150
        return self.sensors.dust_level()

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _set_state(self, new_state: RobotState):
        logger.debug(f"State: {self.state.name} → {new_state.name}")
        self.state = new_state

    @property
    def status(self) -> dict:
        return {
            "state": self.state.name,
            "position_x_mm": self._position_x_mm,
            "position_z_mm": self._position_z_mm,
            "cleaning_pass": self._cleaning_pass,
            "total_passes": self.settings.cleaning.passes,
        }
