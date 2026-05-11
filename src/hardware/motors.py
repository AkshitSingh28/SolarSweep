"""
Motor driver abstractions for Solar Cleaner Robot.

Wraps RPi.GPIO PWM control for:
  - DriveMotor  : L298N/relay-driven DC motor for X-axis rail movement
  - LiftMotor   : DC motor for Z-axis (up/down on steel rods)
  - BrushMotor  : Center rotating cleaning head motor
"""

import logging
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None  # Allow import on non-Pi systems

logger = logging.getLogger(__name__)

PWM_FREQ = 1000  # Hz


class BaseMotor:
    def __init__(self, in1: int, in2: int, en: int, gpio_manager):
        self.in1 = in1
        self.in2 = in2
        self.en = en
        self.gpio = gpio_manager
        self._speed = 0
        self._pwm = None

        if GPIO:
            GPIO.setup(in1, GPIO.OUT)
            GPIO.setup(in2, GPIO.OUT)
            GPIO.setup(en, GPIO.OUT)
            self._pwm = GPIO.PWM(en, PWM_FREQ)
            self._pwm.start(0)

    def set_speed(self, percent: int):
        """Set motor speed 0–100%."""
        self._speed = max(0, min(100, percent))
        if self._pwm:
            self._pwm.ChangeDutyCycle(self._speed)
        logger.debug(f"{self.__class__.__name__} speed set to {self._speed}%")

    def stop(self):
        if GPIO:
            GPIO.output(self.in1, GPIO.LOW)
            GPIO.output(self.in2, GPIO.LOW)
        if self._pwm:
            self._pwm.ChangeDutyCycle(0)
        logger.debug(f"{self.__class__.__name__} stopped.")

    def cleanup(self):
        if self._pwm:
            self._pwm.stop()


class DriveMotor(BaseMotor):
    """Controls forward/backward movement along the rail."""

    def __init__(self, gpio_cfg, gpio_manager):
        super().__init__(
            gpio_cfg.motor_drive_in1,
            gpio_cfg.motor_drive_in2,
            gpio_cfg.motor_drive_en,
            gpio_manager,
        )

    def forward(self):
        if GPIO:
            GPIO.output(self.in1, GPIO.HIGH)
            GPIO.output(self.in2, GPIO.LOW)
        logger.debug("DriveMotor: FORWARD")

    def backward(self):
        if GPIO:
            GPIO.output(self.in1, GPIO.LOW)
            GPIO.output(self.in2, GPIO.HIGH)
        logger.debug("DriveMotor: BACKWARD")


class LiftMotor(BaseMotor):
    """Controls vertical movement on steel rod linear bearings."""

    def __init__(self, gpio_cfg, gpio_manager):
        super().__init__(
            gpio_cfg.motor_lift_in1,
            gpio_cfg.motor_lift_in2,
            gpio_cfg.motor_lift_en,
            gpio_manager,
        )

    def up(self):
        if GPIO:
            GPIO.output(self.in1, GPIO.HIGH)
            GPIO.output(self.in2, GPIO.LOW)
        logger.debug("LiftMotor: UP")

    def down(self):
        if GPIO:
            GPIO.output(self.in1, GPIO.LOW)
            GPIO.output(self.in2, GPIO.HIGH)
        logger.debug("LiftMotor: DOWN")


class BrushMotor(BaseMotor):
    """Controls the center rotating cleaning head (brush + mop)."""

    def __init__(self, gpio_cfg, gpio_manager):
        super().__init__(
            gpio_cfg.motor_brush_in1,
            gpio_cfg.motor_brush_in2,
            gpio_cfg.motor_brush_en,
            gpio_manager,
        )

    def spin(self):
        if GPIO:
            GPIO.output(self.in1, GPIO.HIGH)
            GPIO.output(self.in2, GPIO.LOW)
        logger.debug("BrushMotor: SPINNING")

    def spin_reverse(self):
        if GPIO:
            GPIO.output(self.in1, GPIO.LOW)
            GPIO.output(self.in2, GPIO.HIGH)
        logger.debug("BrushMotor: REVERSE SPIN")
