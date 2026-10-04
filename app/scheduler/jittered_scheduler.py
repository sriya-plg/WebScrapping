"""
Runs a job on a recurring interval with randomized jitter, so a
production deployment doesn't create the exact-periodic traffic pattern
(e.g. precisely every 6h00m) that's itself a bot signature independent
of anything happening inside a single run.

Usage:
    from app.scheduler.jittered_scheduler import run_forever

    def my_job():
        orch = TrackingOrchestrator()
        orch.track_all({...})

    run_forever(my_job)
"""

import random
import time
from datetime import datetime, timedelta

from app.config.settings import settings
from app.core.logger import get_logger

logger = get_logger("scheduler")


def _next_run_delay_seconds() -> float:
    jitter_minutes = random.uniform(-settings.schedule_jitter_minutes, settings.schedule_jitter_minutes)
    delay = timedelta(hours=settings.schedule_interval_hours, minutes=jitter_minutes)
    return max(delay.total_seconds(), 60)  # never less than a minute, just in case


def run_forever(job) -> None:
    logger.info(
        "scheduler_start",
        extra={
            "interval_hours": settings.schedule_interval_hours,
            "jitter_minutes": settings.schedule_jitter_minutes,
        },
    )
    while True:
        started = datetime.now()
        logger.info("scheduled_job_start", extra={"started_at": started.isoformat()})
        try:
            job()
            logger.info("scheduled_job_complete")
        except Exception as e:
            logger.error("scheduled_job_failed", extra={"error": str(e)})

        delay = _next_run_delay_seconds()
        next_run = datetime.now() + timedelta(seconds=delay)
        logger.info(
            "scheduler_sleeping",
            extra={"next_run_at": next_run.isoformat(), "sleep_seconds": round(delay)},
        )
        time.sleep(delay)
