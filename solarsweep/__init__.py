"""SolarSweep — rail-guided solar panel cleaning robot.

Public entry points::

    from solarsweep import load_settings, make_board, SolarSweepRobot

    settings = load_settings("config/settings.yaml")
    with make_board(settings, simulate=True) as board:
        robot = SolarSweepRobot(board, settings)
        summary = robot.run_cycle("full_cycle")
"""

from .config import ConfigError, Settings, load_settings
from .control import RobotState, SolarSweepRobot
from .hal import make_board
from .safety import SafetyAbort, StopReason

__version__ = "2.0.0"

__all__ = [
    "ConfigError",
    "RobotState",
    "SafetyAbort",
    "Settings",
    "SolarSweepRobot",
    "StopReason",
    "__version__",
    "load_settings",
    "make_board",
]
