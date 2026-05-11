"""Relay controller for water pump and auxiliary outputs."""
import logging
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

logger = logging.getLogger(__name__)


class RelayController:
    def __init__(self, gpio_cfg, gpio_manager):
        self.pump_pin = gpio_cfg.relay_water_pump
        self.aux_pin = gpio_cfg.relay_aux
        if GPIO:
            GPIO.setup(self.pump_pin, GPIO.OUT, initial=GPIO.HIGH)  # Active LOW relay
            GPIO.setup(self.aux_pin, GPIO.OUT, initial=GPIO.HIGH)

    def water_pump_on(self):
        if GPIO:
            GPIO.output(self.pump_pin, GPIO.LOW)
        logger.info("Water pump ON")

    def water_pump_off(self):
        if GPIO:
            GPIO.output(self.pump_pin, GPIO.HIGH)
        logger.info("Water pump OFF")

    def aux_on(self):
        if GPIO:
            GPIO.output(self.aux_pin, GPIO.LOW)

    def aux_off(self):
        if GPIO:
            GPIO.output(self.aux_pin, GPIO.HIGH)

    def all_off(self):
        self.water_pump_off()
        self.aux_off()
