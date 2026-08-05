"""The Raspberry Pi backend, against a fake GPIO module.

This code cannot run on a laptop and will not be exercised until it is on a
roof, which is exactly the situation that produced v1. A fake `RPi.GPIO` lets
the parts most likely to be wrong — pin polarity, relay sense, direction
signs, and the shutdown path — be checked here instead of with a multimeter.

It does not prove the wiring. It proves the software's half of the contract.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from solarsweep.hal import InputId, MotorId, RelayId
from solarsweep.hal.base import HardwareError


class FakePWM:
    def __init__(self, pin, freq):
        self.pin = pin
        self.freq = freq
        self.duty = None
        self.stopped = False

    def start(self, duty):
        self.duty = duty

    def ChangeDutyCycle(self, duty):  # noqa: N802 - mirrors the RPi.GPIO API
        self.duty = duty

    def stop(self):
        self.stopped = True


class FakeGPIO:
    BCM, OUT, IN, HIGH, LOW = "BCM", "OUT", "IN", 1, 0
    PUD_UP, PUD_DOWN, RISING = "PUD_UP", "PUD_DOWN", "RISING"

    def __init__(self):
        self.mode = None
        self.levels: dict[int, int] = {}
        self.modes: dict[int, str] = {}
        self.pulls: dict[int, str] = {}
        self.pwms: dict[int, FakePWM] = {}
        self.callbacks: dict[int, object] = {}
        self.cleaned = False

    def setmode(self, mode):
        self.mode = mode

    def setwarnings(self, _flag):
        pass

    def setup(self, pin, direction, initial=None, pull_up_down=None):
        self.modes[pin] = direction
        if initial is not None:
            self.levels[pin] = initial
        if pull_up_down is not None:
            self.pulls[pin] = pull_up_down
            # A pulled-up input with nothing attached reads HIGH.
            self.levels.setdefault(pin, self.HIGH if pull_up_down == self.PUD_UP else self.LOW)

    def output(self, pin, level):
        assert self.modes.get(pin) == self.OUT, f"wrote to BCM {pin} without setting it OUT"
        self.levels[pin] = level

    def input(self, pin):
        return self.levels.get(pin, self.LOW)

    def PWM(self, pin, freq):  # noqa: N802 - mirrors the RPi.GPIO API
        pwm = FakePWM(pin, freq)
        self.pwms[pin] = pwm
        return pwm

    def add_event_detect(self, pin, edge, callback=None):
        self.callbacks[pin] = callback

    def cleanup(self):
        self.cleaned = True


@pytest.fixture
def gpio(monkeypatch):
    from solarsweep.hal import rpi

    fake = FakeGPIO()
    monkeypatch.setattr(rpi, "GPIO", fake)
    monkeypatch.setattr(rpi, "spidev", None)
    return fake


@pytest.fixture
def pi_board(gpio, settings):
    from solarsweep.hal.rpi import RaspberryPiBoard

    board = RaspberryPiBoard(settings)
    yield board
    board.close()


# -- Startup ---------------------------------------------------------------


def test_missing_gpio_library_fails_loudly(monkeypatch, settings):
    """Never silently pretend to drive hardware."""
    from solarsweep.hal import rpi

    monkeypatch.setattr(rpi, "GPIO", None)
    with pytest.raises(HardwareError, match="--sim"):
        rpi.RaspberryPiBoard(settings)


def test_relays_are_initialised_open(pi_board, gpio, settings):
    """Active-LOW boards must be driven HIGH the instant we take the pins."""
    assert gpio.levels[settings.pins.relay_pump] == gpio.HIGH
    assert gpio.levels[settings.pins.relay_aux] == gpio.HIGH


def test_motors_are_initialised_stopped(pi_board, gpio, settings):
    p = settings.pins
    assert gpio.levels[p.drive_in1] == gpio.LOW
    assert gpio.levels[p.drive_in2] == gpio.LOW
    assert gpio.pwms[p.drive_en].duty == 0


def test_switch_inputs_get_pull_ups(pi_board, gpio, settings):
    for pin in (settings.pins.limit_home, settings.pins.limit_far,
                settings.pins.estop_button):
        assert gpio.pulls[pin] == gpio.PUD_UP


# -- Motor direction -------------------------------------------------------


@pytest.mark.parametrize(
    "duty, in1, in2, pwm",
    [
        (60, 1, 0, 60),    # positive = toward the far end
        (-60, 0, 1, 60),   # negative = toward home
        (0, 0, 0, 0),      # zero = coast
    ],
)
def test_signed_duty_maps_to_direction_pins(pi_board, gpio, settings, duty, in1, in2, pwm):
    pi_board.set_motor(MotorId.DRIVE, duty)
    p = settings.pins
    assert gpio.levels[p.drive_in1] == in1
    assert gpio.levels[p.drive_in2] == in2
    assert gpio.pwms[p.drive_en].duty == pwm


def test_duty_is_clamped(pi_board, gpio, settings):
    pi_board.set_motor(MotorId.BRUSH, 500)
    assert gpio.pwms[settings.pins.brush_en].duty == 100
    pi_board.set_motor(MotorId.BRUSH, -500)
    assert gpio.pwms[settings.pins.brush_en].duty == 100


def test_brake_shorts_the_winding(pi_board, gpio, settings):
    """Both inputs high, not both low — the difference between holding the
    carriage on a tilted panel and letting it roll."""
    pi_board.brake_motor(MotorId.DRIVE)
    p = settings.pins
    assert gpio.levels[p.drive_in1] == gpio.HIGH
    assert gpio.levels[p.drive_in2] == gpio.HIGH


# -- Relay polarity --------------------------------------------------------


def test_active_low_relay_polarity(pi_board, gpio, settings):
    pi_board.set_relay(RelayId.PUMP, True)
    assert gpio.levels[settings.pins.relay_pump] == gpio.LOW   # ON
    pi_board.set_relay(RelayId.PUMP, False)
    assert gpio.levels[settings.pins.relay_pump] == gpio.HIGH  # OFF


def test_active_high_relay_polarity(gpio, settings):
    from solarsweep.hal.rpi import RaspberryPiBoard

    settings = replace(settings, pins=replace(settings.pins, relays_active_low=False))
    board = RaspberryPiBoard(settings)
    try:
        assert gpio.levels[settings.pins.relay_pump] == gpio.LOW  # OFF at init
        board.set_relay(RelayId.PUMP, True)
        assert gpio.levels[settings.pins.relay_pump] == gpio.HIGH
    finally:
        board.close()


# -- Input polarity --------------------------------------------------------


def test_normally_closed_switch_reads_asserted_when_the_wire_breaks(pi_board, gpio,
                                                                    settings):
    """The whole reason for wiring NC. An open circuit — cut wire, corroded
    contact, connector shaken loose — must read the same as 'triggered'."""
    p = settings.pins
    gpio.levels[p.limit_home] = gpio.LOW      # switch closed to ground: at rest
    assert pi_board.read_input(InputId.LIMIT_HOME) is False

    gpio.levels[p.limit_home] = gpio.HIGH     # pressed, or the wire is broken
    assert pi_board.read_input(InputId.LIMIT_HOME) is True


def test_normally_open_switch_has_the_opposite_sense(gpio, settings):
    from solarsweep.hal.rpi import RaspberryPiBoard

    settings = replace(settings, pins=replace(settings.pins,
                                              limits_normally_closed=False))
    board = RaspberryPiBoard(settings)
    try:
        gpio.levels[settings.pins.limit_home] = gpio.HIGH
        assert board.read_input(InputId.LIMIT_HOME) is False
        gpio.levels[settings.pins.limit_home] = gpio.LOW
        assert board.read_input(InputId.LIMIT_HOME) is True
    finally:
        board.close()


def test_estop_polarity_is_configured_separately_from_the_limits(pi_board, gpio,
                                                                 settings):
    gpio.levels[settings.pins.estop_button] = gpio.LOW
    assert pi_board.read_input(InputId.ESTOP) is False
    gpio.levels[settings.pins.estop_button] = gpio.HIGH
    assert pi_board.read_input(InputId.ESTOP) is True


# -- Encoder ---------------------------------------------------------------


def test_encoder_counts_edges(pi_board, gpio, settings):
    callback = gpio.callbacks[settings.pins.encoder_a]
    assert callback is not None
    gpio.levels[settings.pins.encoder_b] = gpio.LOW  # forward
    for _ in range(24):
        callback(settings.pins.encoder_a)
    expected = 24 / settings.drive.encoder_ticks_per_mm
    assert pi_board.read_encoder_mm() == pytest.approx(expected)


def test_encoder_direction_follows_channel_b(pi_board, gpio, settings):
    callback = gpio.callbacks[settings.pins.encoder_a]
    gpio.levels[settings.pins.encoder_b] = gpio.HIGH  # reverse
    for _ in range(10):
        callback(settings.pins.encoder_a)
    assert pi_board.read_encoder_mm() < 0


def test_reset_encoder_rezeroes(pi_board, gpio, settings):
    callback = gpio.callbacks[settings.pins.encoder_a]
    gpio.levels[settings.pins.encoder_b] = gpio.LOW
    for _ in range(12):
        callback(settings.pins.encoder_a)
    pi_board.reset_encoder()
    assert pi_board.read_encoder_mm() == pytest.approx(0.0)


def test_no_encoder_configured_reads_none(gpio, open_loop_settings):
    from solarsweep.hal.rpi import RaspberryPiBoard

    settings = replace(open_loop_settings,
                       pins=replace(open_loop_settings.pins, encoder_a=-1, encoder_b=-1))
    board = RaspberryPiBoard(settings)
    try:
        assert board.read_encoder_mm() is None
        assert board.info.has_encoder is False
    finally:
        board.close()


# -- Shutdown --------------------------------------------------------------


def test_all_stop_de_energises_everything(pi_board, gpio, settings):
    p = settings.pins
    pi_board.set_motor(MotorId.DRIVE, 80)
    pi_board.set_motor(MotorId.BRUSH, 80)
    pi_board.set_relay(RelayId.PUMP, True)

    pi_board.all_stop()

    assert gpio.pwms[p.drive_en].duty == 0
    assert gpio.pwms[p.brush_en].duty == 0
    assert gpio.levels[p.drive_in1] == gpio.LOW
    assert gpio.levels[p.drive_in2] == gpio.LOW
    assert gpio.levels[p.relay_pump] == gpio.HIGH  # relay open


def test_all_stop_never_raises_even_when_gpio_misbehaves(pi_board, gpio):
    """It runs from except blocks, signal handlers and atexit. If it can
    raise, it can mask the original fault and leave a motor running."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("GPIO went away")

    gpio.output = explode
    pi_board.all_stop()  # must not raise


def test_close_is_idempotent_and_cleans_up(pi_board, gpio, settings):
    pi_board.close()
    assert gpio.cleaned
    assert gpio.pwms[settings.pins.drive_en].stopped
    pi_board.close()  # second call is a no-op, not an error


def test_using_a_closed_board_is_an_error(pi_board):
    pi_board.close()
    with pytest.raises(HardwareError, match="closed"):
        pi_board.set_motor(MotorId.DRIVE, 50)


def test_dust_returns_zero_without_spi(pi_board):
    assert pi_board.read_dust() == 0
