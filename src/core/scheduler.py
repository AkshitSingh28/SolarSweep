"""
Scheduled cleaning jobs using APScheduler with cron expressions.
Reads schedule from settings.yaml and triggers robot cleaning cycles.
"""

import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


class CleaningScheduler:
    def __init__(self, robot, settings):
        self.robot = robot
        self.settings = settings
        self.scheduler = BlockingScheduler(timezone="Asia/Kolkata")

    def _make_job(self, mode: str):
        def job():
            logger.info(f"Scheduled job triggered: mode={mode}")
            if mode == "full_cycle":
                self.robot.run_full_cycle()
            elif mode == "quick_pass":
                self.robot.run_quick_pass()
        return job

    def run(self):
        if not self.settings.scheduler_jobs:
            logger.warning("No scheduler jobs configured.")
            return

        for job_cfg in self.settings.scheduler_jobs:
            cron_expr = job_cfg.get("cron", "0 6 * * *")
            mode = job_cfg.get("mode", "full_cycle")
            minute, hour, day, month, day_of_week = cron_expr.split()
            trigger = CronTrigger(
                minute=minute, hour=hour,
                day=day, month=month, day_of_week=day_of_week
            )
            self.scheduler.add_job(self._make_job(mode), trigger=trigger, name=mode)
            logger.info(f"Scheduled job '{mode}' at cron: {cron_expr}")

        logger.info("Scheduler running. Press Ctrl+C to stop.")
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler stopped.")
