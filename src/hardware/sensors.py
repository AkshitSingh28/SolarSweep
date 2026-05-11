"""
Sensor hub for Solar Cleaner Robot.

Manages:
  - 4x limit switches (front/rear/top/bottom)
  - Rain sensor (digital)
  - Dust/particulate sensor (analog via ADC)
  - IR obstacle detector
"""

import logging
try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

try:
    import spidev  # For MCP3008 ADC
    SPI_AVAILABLE = True
except ImportError:
    SPI_AVAILABLE = False

logger = logging.getLogger(__name__)


class SensorHub:
    def __init__(self, gpio_cfg, gpio_manager):
        self.cfg = gpio_cfg
        self._spi = None

        if GPIO:
            for pin in [
                gpio_cfg.limit_switch_front,
                gpio_cfg.limit_switch_rear,
                gpio_cfg.limit_switch_top,
                gpio_cfg.limit_switch_bottom,
                gpio_cfg.rain_sensor,
                gpio_cfg.ir_obstacle,
            ]:
                GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        if SPI_AVAILABLE:
            self._spi = spidev.SpiDev()
            self._spi.open(0, 0)
            self._spi.max_speed_hz = 1350000

    # -- Limit switches (active LOW with pull-up) --

    def limit_front(self) -> bool:
        if not GPIO:
            return False
        return GPIO.input(self.cfg.limit_switch_front) == GPIO.LOW

    def limit_rear(self) -> bool:
        if not GPIO:
            return False
        return GPIO.input(self.cfg.limit_switch_rear) == GPIO.LOW

    def limit_top(self) -> bool:
        if not GPIO:
            return False
        return GPIO.input(self.cfg.limit_switch_top) == GPIO.LOW

    def limit_bottom(self) -> bool:
        if not GPIO:
            return False
        return GPIO.input(self.cfg.limit_switch_bottom) == GPIO.LOW

    # -- Environmental sensors --

    def rain_detected(self) -> bool:
        if not GPIO:
            return False
        return GPIO.input(self.cfg.rain_sensor) == GPIO.LOW

    def obstacle_detected(self) -> bool:
        if not GPIO:
            return False
        return GPIO.input(self.cfg.ir_obstacle) == GPIO.LOW

    def dust_level(self) -> int:
        """Read dust sensor via MCP3008 ADC. Returns 0-1023."""
        if not self._spi:
            return 0
        channel = self.cfg.dust_sensor_adc
        adc = self._spi.xfer2([1, (8 + channel) << 4, 0])
        data = ((adc[1] & 3) << 8) + adc[2]
        logger.debug(f"Dust ADC reading: {data}")
        return data

    def cleanup(self):
        if self._spi:
            self._spi.close()
