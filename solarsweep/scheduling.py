"""Cron-driven cycles.

v1 used ``BlockingScheduler``, which meant the scheduler and the web dashboard
could never run in the same process — and since ``main.py`` was empty, nothing
ever tried. This uses a background scheduler so ``solarsweep serve`` runs both,
and it refuses to stack a second cycle on top of one already running.

Scheduled runs are *unattended*, which the robot treats more strictly than a
manual run: no encoder or current sensing means it declines to start rather
than risk grinding a gearbox with nobody watching.
"""

from __future__ import annotations

import logging
import threading

from .config import Settings
from .control import CycleRefused, SolarSweepRobot

logger = logging.getLogger(__name__)


class CleaningScheduler:
    def __init__(self, robot: SolarSweepRobot, settings: Settings) -> None:
        self._robot = robot
        self._settings = settings
        self._scheduler = None
        self._run_lock = threading.Lock()
        self._running = False

    @property
    def busy(self) -> bool:
        return self._running

    def start(self) -> None:
        cfg = self._settings.scheduler
        if not cfg.enabled:
            logger.info("scheduler disabled in settings")
            return
        if not cfg.jobs:
            logger.warning("scheduler enabled but no jobs are configured")
            return

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "APScheduler is required for scheduled cleaning: pip install APScheduler"
            ) from exc

        self._scheduler = BackgroundScheduler(timezone=cfg.timezone)
        for job in cfg.jobs:
            minute, hour, day, month, dow = job.cron.split()
            self._scheduler.add_job(
                self._make_job(job.mode),
                trigger=CronTrigger(
                    minute=minute, hour=hour, day=day, month=month, day_of_week=dow
                ),
                name=f"{job.mode} @ {job.cron}",
                # A missed run (Pi was asleep, cycle overran) should be dropped,
                # not fired at 3am when it is dark and nobody is watching.
                misfire_grace_time=1800,
                coalesce=True,
                max_instances=1,
            )
            logger.info("scheduled %s at cron '%s' (%s)", job.mode, job.cron, cfg.timezone)

        self._scheduler.start()

    def _make_job(self, mode: str):
        def job() -> None:
            if not self._run_lock.acquire(blocking=False):
                logger.warning("skipping scheduled %s: a cycle is already running", mode)
                return
            self._running = True
            try:
                self._robot.unattended = True
                summary = self._robot.run_cycle(mode)
                logger.info("scheduled %s finished: %s", mode, summary.outcome)
            except CycleRefused as exc:
                logger.info("scheduled %s declined: %s", mode, exc)
            except Exception:  # pragma: no cover
                logger.exception("scheduled %s crashed", mode)
            finally:
                self._running = False
                self._run_lock.release()

        return job

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("scheduler stopped")

    def next_runs(self) -> list[dict]:
        if self._scheduler is None:
            return []
        return [
            {
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in self._scheduler.get_jobs()
        ]
