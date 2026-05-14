import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.sync import run_sync

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


def start_scheduler() -> None:
    scheduler.add_job(
        run_sync,
        trigger=CronTrigger(hour=settings.sync_cron_hour, minute=settings.sync_cron_minute),
        id="daily_github_sync",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("scheduler started: daily sync at %02d:%02d UTC", settings.sync_cron_hour, settings.sync_cron_minute)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
