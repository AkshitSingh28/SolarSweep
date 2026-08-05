"""Logging configuration.

v1 called ``logger.info`` all over the place and never configured logging, so
every one of those calls went nowhere. On a machine you cannot stand next to,
the log *is* the instrument panel.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import LoggingConfig

_CONSOLE_FMT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"
_FILE_FMT = "%(asctime)s %(levelname)-8s %(name)s %(filename)s:%(lineno)d %(message)s"


def setup_logging(cfg: LoggingConfig, verbose: bool = False, quiet: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, cfg.level.upper())

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING if quiet else level)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    if cfg.file:
        try:
            path = Path(cfg.file)
            path.parent.mkdir(parents=True, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count
            )
            rotating.setLevel(logging.DEBUG)
            rotating.setFormatter(logging.Formatter(_FILE_FMT))
            root.addHandler(rotating)
        except OSError as exc:
            # A read-only SD card must not stop the robot from running.
            root.warning("file logging disabled: %s", exc)

    # These are chatty and never tell us anything about the robot.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
