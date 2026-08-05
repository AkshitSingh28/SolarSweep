"""Hardware backends. Pick one with :func:`make_board`."""

from __future__ import annotations

import logging

from ..config import Settings
from .base import (
    Board,
    BoardInfo,
    Clock,
    HardwareError,
    InputId,
    MotorId,
    RealClock,
    RelayId,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Board", "BoardInfo", "Clock", "HardwareError", "InputId", "MotorId",
    "RealClock", "RelayId", "make_board",
]


def make_board(settings: Settings, simulate: bool | None = None, seed: int = 0) -> Board:
    """Build the right backend.

    ``simulate=None`` auto-detects: real hardware when RPi.GPIO imports,
    simulator otherwise. Auto-detection is a convenience for development —
    the systemd unit and the CLI both pass the flag explicitly so that a
    missing library on the Pi fails loudly instead of quietly pretending to
    clean a panel.
    """
    from .sim import SimBoard

    if simulate is True:
        return SimBoard(settings, seed=seed)

    from .rpi import GPIO, RaspberryPiBoard

    if simulate is False:
        return RaspberryPiBoard(settings)

    if GPIO is not None:
        return RaspberryPiBoard(settings)
    logger.warning("No GPIO library found — falling back to the simulator")
    return SimBoard(settings, seed=seed)
