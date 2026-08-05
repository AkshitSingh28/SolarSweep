"""Scheduled cycles.

v1 used a BlockingScheduler, so the scheduler and the dashboard could never
share a process — and `main.py` was empty, so neither ever started. The parts
worth testing here are the ones that decide *not* to run.
"""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from solarsweep.config import ScheduleEntry, SchedulerConfig
from solarsweep.scheduling import CleaningScheduler


@pytest.fixture
def scheduled(settings):
    return replace(
        settings,
        scheduler=SchedulerConfig(
            enabled=True,
            timezone="Asia/Kolkata",
            jobs=(ScheduleEntry(cron="30 6 * * *", mode="full_cycle"),
                  ScheduleEntry(cron="0 18 * * 0", mode="quick_pass")),
        ),
    )


def test_disabled_scheduler_starts_nothing(robot, settings):
    scheduler = CleaningScheduler(robot, settings)
    scheduler.start()
    assert scheduler.next_runs() == []
    scheduler.shutdown()


def test_enabled_scheduler_registers_every_job(robot, scheduled):
    scheduler = CleaningScheduler(robot, scheduled)
    scheduler.start()
    try:
        jobs = scheduler.next_runs()
        assert len(jobs) == 2
        assert all(job["next_run"] for job in jobs)
    finally:
        scheduler.shutdown()


def test_enabled_but_jobless_scheduler_is_harmless(robot, settings):
    settings = replace(settings, scheduler=SchedulerConfig(enabled=True, jobs=()))
    scheduler = CleaningScheduler(robot, settings)
    scheduler.start()
    assert scheduler.next_runs() == []
    scheduler.shutdown()


def test_a_scheduled_run_is_marked_unattended(robot, scheduled):
    """Unattended is stricter: without an encoder the robot declines rather
    than risk grinding a gearbox with nobody watching."""
    scheduler = CleaningScheduler(robot, scheduled)
    job = scheduler._make_job("quick_pass")
    assert robot.unattended is False
    job()
    assert robot.unattended is True


def test_the_scheduler_will_not_stack_two_cycles(robot, scheduled):
    scheduler = CleaningScheduler(robot, scheduled)
    job = scheduler._make_job("full_cycle")

    first = threading.Thread(target=job, daemon=True)
    first.start()
    # Wait until the first cycle really holds the lock.
    for _ in range(2000):
        if scheduler.busy:
            break
        threading.Event().wait(0.001)
    assert scheduler.busy

    job()  # second trigger while the first is mid-cycle: dropped, not queued
    first.join(timeout=10)
    assert not scheduler.busy


def test_a_refused_cycle_does_not_crash_the_job(robot, scheduled):
    """`min_hours_between_runs` makes the second call a refusal. A refusal
    must not propagate out of the job and kill the scheduler thread."""
    scheduler = CleaningScheduler(robot, scheduled)
    job = scheduler._make_job("quick_pass")
    job()
    job()  # too soon — refused inside, no exception out
    assert not scheduler.busy


def test_shutdown_is_safe_before_start(robot, settings):
    CleaningScheduler(robot, settings).shutdown()
