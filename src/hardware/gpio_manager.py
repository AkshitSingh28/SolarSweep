"""GPIO board initialization and cleanup manager."""
import logging
import atexit

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

logger = logging.getLogger(__name__)


class GPIOManager:
    def __init__(self, gpio_cfg):
        if GPIO:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            atexit.register(self.cleanup)
            logger.info("GPIO initialized in BCM mode.")
        else:
            logger.warning("RPi.GPIO not available — running in simulation mode.")

    def cleanup(self):
        if GPIO:
            GPIO.cleanup()
            logger.info("GPIO cleaned up.")
