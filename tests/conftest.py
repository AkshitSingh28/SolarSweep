"""Shared fixtures.

Everything runs against the simulator at ``time_scale=0``, so a full cleaning
cycle — two 1.8 m passes, homing, referencing and parking — takes milliseconds
of wall clock. That is the whole point: the v1 prototype's safety logic could
only have been exercised by standing next to the machine.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from solarsweep.config import PinConfig, Settings, SimConfig
from solarsweep.control import SolarSweepRobot
from solarsweep.hal.sim import SimBoard


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A valid machine with an encoder fitted, logging into tmp_path."""
    base = Settings()
    base = replace(
        base,
        sim=SimConfig(time_scale=0.0),
        drive=replace(base.drive, encoder_ticks_per_mm=2.4),
        # Matches config/settings.yaml: the recommended build has the encoder
        # fitted. open_loop_settings below is the as-photographed machine.
        pins=PinConfig(encoder_a=25, encoder_b=26),
        logging=replace(
            base.logging,
            file=str(tmp_path / "solarsweep.log"),
            telemetry_dir=str(tmp_path / "runs"),
        ),
    )
    base.validate()
    return base


@pytest.fixture
def open_loop_settings(settings) -> Settings:
    """The prototype as photographed: no encoder, no current sensing."""
    out = replace(
        settings,
        drive=replace(settings.drive, encoder_ticks_per_mm=0.0),
        pins=replace(settings.pins, encoder_a=-1, encoder_b=-1),
    )
    out.validate()
    return out


@pytest.fixture
def board(settings) -> SimBoard:
    board = SimBoard(settings)
    yield board
    board.close()


@pytest.fixture
def robot(settings, board) -> SolarSweepRobot:
    robot = SolarSweepRobot(board, settings)
    yield robot
    robot.monitor.stop()


def make_robot(settings, fault: str = "", **overrides) -> tuple[SolarSweepRobot, SimBoard]:
    """Build a robot with a fault injected. Caller is responsible for cleanup."""
    if fault:
        settings = replace(settings, sim=replace(settings.sim, fault=fault))
    if overrides:
        settings = replace(settings, **overrides)
    settings.validate()
    board = SimBoard(settings)
    return SolarSweepRobot(board, settings), board
