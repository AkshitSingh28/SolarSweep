"""Raspberry Pi backend.

Targets the Pi Zero 2 W in the prototype's control box, driving an L298N-class
H-bridge for the rail and brush motors and a 2-channel active-LOW relay board
for the pump.

Two things here are not obvious and both bit the v1 design:

1. **Relay state at boot.** Between power-on and this module running, the Pi's
   pins are inputs with weak pull-downs. An active-LOW relay board reads that
   as ON and energises the pump. ``initial=`` below closes the window once we
   are running, but the real fix is a 10k pull-up from each relay input to
   +3V3 — see docs/WIRING.md. Do not skip it; the failure mode is a pump
   running unattended on a roof.

2. **Normally-closed switches.** Limit switches and the e-stop are wired NC so
   that a cut or corroded wire reads the same as "triggered". Polarity is
   resolved here so the state machine only ever sees "asserted / not".
"""

from __future__ import annotations

import atexit
import logging
import threading

from ..config import Settings
from .base import Board, BoardInfo, Clock, HardwareError, InputId, MotorId, RealClock, RelayId

logger = logging.getLogger(__name__)

PWM_FREQ_HZ = 1000

try:  # pragma: no cover - depends on the host
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError):  # RuntimeError: importing off-Pi
    GPIO = None

try:  # pragma: no cover
    import spidev
except ImportError:
    spidev = None


class RaspberryPiBoard(Board):
    def __init__(self, settings: Settings) -> None:
        if GPIO is None:
            raise HardwareError(
                "RPi.GPIO is not available. Install it on the Pi "
                "(`pip install RPi.GPIO`), or run with --sim on a laptop."
            )
        self._settings = settings
        self._pins = settings.pins
        self._clock = RealClock()
        self._lock = threading.Lock()
        self._pwm: dict[MotorId, object] = {}
        self._dirs: dict[MotorId, tuple[int, int]] = {}
        self._encoder_ticks = 0
        self._encoder_origin = 0
        self._spi = None
        self._closed = False

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        self._setup_motors()
        self._setup_relays()
        self._setup_inputs()
        self._setup_encoder()
        self._setup_adc()

        # Belt and braces: whatever happens to the process, outputs die.
        atexit.register(self._safe_shutdown)
        logger.info("Raspberry Pi board initialised (BCM numbering)")

    # -- Setup -------------------------------------------------------------

    def _setup_motors(self) -> None:
        p = self._pins
        spec = {
            MotorId.DRIVE: (p.drive_in1, p.drive_in2, p.drive_en),
            MotorId.BRUSH: (p.brush_in1, p.brush_in2, p.brush_en),
        }
        for motor, (in1, in2, en) in spec.items():
            GPIO.setup(in1, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(in2, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(en, GPIO.OUT, initial=GPIO.LOW)
            pwm = GPIO.PWM(en, PWM_FREQ_HZ)
            pwm.start(0)
            self._pwm[motor] = pwm
            self._dirs[motor] = (in1, in2)

    def _setup_relays(self) -> None:
        off = GPIO.HIGH if self._pins.relays_active_low else GPIO.LOW
        for pin in (self._pins.relay_pump, self._pins.relay_aux):
            GPIO.setup(pin, GPIO.OUT, initial=off)

    def _setup_inputs(self) -> None:
        p = self._pins
        # NC switches idle closed to ground, so they need a pull-up and read
        # HIGH when the wire breaks or the switch opens (= asserted).
        for pin in (p.limit_home, p.limit_far, p.estop_button,
                    p.rain_sensor, p.obstacle_sensor, p.water_pressure):
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    def _setup_encoder(self) -> None:
        p = self._pins
        if p.encoder_a < 0:
            return
        GPIO.setup(p.encoder_a, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        if p.encoder_b >= 0:
            GPIO.setup(p.encoder_b, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        def _on_edge(_channel: int) -> None:
            # Quadrature when B is fitted, otherwise count magnitude only and
            # take the sign from the commanded direction.
            step = 1
            if p.encoder_b >= 0 and GPIO.input(p.encoder_b) != GPIO.LOW:
                step = -1
            self._encoder_ticks += step

        GPIO.add_event_detect(p.encoder_a, GPIO.RISING, callback=_on_edge)

    def _setup_adc(self) -> None:
        if spidev is None:
            logger.warning("spidev unavailable — dust readings will report 0")
            return
        try:
            self._spi = spidev.SpiDev()
            self._spi.open(0, 0)
            self._spi.max_speed_hz = 1_350_000
        except Exception as exc:  # pragma: no cover
            logger.warning("MCP3008 not reachable (%s) — dust readings will report 0", exc)
            self._spi = None

    # -- Board interface ---------------------------------------------------

    @property
    def info(self) -> BoardInfo:
        return BoardInfo(
            backend="rpi",
            simulated=False,
            has_encoder=self._pins.encoder_a >= 0 and self._settings.drive.closed_loop,
            has_current_sense=False,
        )

    @property
    def clock(self) -> Clock:
        return self._clock

    def set_motor(self, motor: MotorId, duty_pct: float) -> None:
        self._require_open()
        duty = max(-100.0, min(100.0, float(duty_pct)))
        in1, in2 = self._dirs[motor]
        with self._lock:
            if duty > 0:
                GPIO.output(in1, GPIO.HIGH)
                GPIO.output(in2, GPIO.LOW)
            elif duty < 0:
                GPIO.output(in1, GPIO.LOW)
                GPIO.output(in2, GPIO.HIGH)
            else:
                # Coast: both low, PWM to zero.
                GPIO.output(in1, GPIO.LOW)
                GPIO.output(in2, GPIO.LOW)
            self._pwm[motor].ChangeDutyCycle(abs(duty))

    def brake_motor(self, motor: MotorId) -> None:
        self._require_open()
        in1, in2 = self._dirs[motor]
        with self._lock:
            # Both inputs high shorts the winding through the bridge.
            GPIO.output(in1, GPIO.HIGH)
            GPIO.output(in2, GPIO.HIGH)
            self._pwm[motor].ChangeDutyCycle(100)

    def set_relay(self, relay: RelayId, on: bool) -> None:
        self._require_open()
        pin = self._pins.relay_pump if relay is RelayId.PUMP else self._pins.relay_aux
        if self._pins.relays_active_low:
            level = GPIO.LOW if on else GPIO.HIGH
        else:
            level = GPIO.HIGH if on else GPIO.LOW
        with self._lock:
            GPIO.output(pin, level)

    def read_input(self, channel: InputId) -> bool:
        p = self._pins
        mapping = {
            InputId.LIMIT_HOME: (p.limit_home, p.limits_normally_closed),
            InputId.LIMIT_FAR: (p.limit_far, p.limits_normally_closed),
            InputId.ESTOP: (p.estop_button, p.estop_normally_closed),
            InputId.RAIN: (p.rain_sensor, False),
            InputId.OBSTACLE: (p.obstacle_sensor, False),
            InputId.WATER_PRESSURE: (p.water_pressure, False),
        }
        pin, normally_closed = mapping[channel]
        level = GPIO.input(pin)
        # NC idles closed to ground (LOW), so asserted — or a broken wire —
        # reads HIGH. NO is the other way round.
        level_when_asserted = GPIO.HIGH if normally_closed else GPIO.LOW
        asserted = level == level_when_asserted
        if channel is InputId.WATER_PRESSURE:
            # This one is inverted in meaning: asserted means "pressure OK".
            return not asserted if not normally_closed else asserted
        return asserted

    def read_dust(self) -> int:
        if self._spi is None:
            return 0
        channel = 0
        raw = self._spi.xfer2([1, (8 + channel) << 4, 0])
        return ((raw[1] & 0x03) << 8) + raw[2]

    def read_encoder_mm(self) -> float | None:
        if self._pins.encoder_a < 0 or not self._settings.drive.closed_loop:
            return None
        ticks = self._encoder_ticks - self._encoder_origin
        return ticks / self._settings.drive.encoder_ticks_per_mm

    def reset_encoder(self) -> None:
        self._encoder_origin = self._encoder_ticks

    def all_stop(self) -> None:
        """De-energise everything. Deliberately defensive: this runs from
        exception handlers, signal handlers and atexit, so it must not raise."""
        if GPIO is None:
            return
        off = GPIO.HIGH if self._pins.relays_active_low else GPIO.LOW
        for motor, (in1, in2) in self._dirs.items():
            try:
                self._pwm[motor].ChangeDutyCycle(0)
                GPIO.output(in1, GPIO.LOW)
                GPIO.output(in2, GPIO.LOW)
            except Exception:  # pragma: no cover
                logger.exception("failed to stop %s", motor)
        for pin in (self._pins.relay_pump, self._pins.relay_aux):
            try:
                GPIO.output(pin, off)
            except Exception:  # pragma: no cover
                logger.exception("failed to open relay on BCM %s", pin)

    def close(self) -> None:
        self._safe_shutdown()

    def _safe_shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.all_stop()
        for pwm in self._pwm.values():
            try:
                pwm.stop()
            except Exception:  # pragma: no cover
                pass
        if self._spi is not None:
            try:
                self._spi.close()
            except Exception:  # pragma: no cover
                pass
        try:
            GPIO.cleanup()
        except Exception:  # pragma: no cover
            pass
        logger.info("Raspberry Pi board shut down")

    def _require_open(self) -> None:
        if self._closed:
            raise HardwareError("board is closed")
