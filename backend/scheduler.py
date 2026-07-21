import asyncio
import logging
import uuid
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from scrapers import run_scrape, make_run
from storage import read_db

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
_runs: dict = {}  # shared with main.py after init


def setup_scheduler(shared_runs: dict) -> None:
    global _runs
    _runs = shared_runs
    _scheduler.add_job(
        _daily_job,
        CronTrigger(hour=settings.scheduler_hour, minute=settings.scheduler_minute),
        id="daily_refresh",
        replace_existing=True,
    )
    _scheduler.start()


def get_scheduler_status() -> dict:
    job = _scheduler.get_job("daily_refresh")
    return {
        "running": _scheduler.running,
        "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


async def _daily_job() -> None:
    """Refresh all sessions for all users."""
    try:
        db = read_db()
        for username, user_data in db.get("users", {}).items():
            for session_id, session in user_data.get("sessions", {}).items():
                run_id = str(uuid.uuid4())
                _runs[run_id] = make_run(
                    run_id,
                    session_id=session_id,
                    message="Actualización diaria...",
                    triggered_by="scheduler",
                )
                try:
                    await run_scrape(
                        session=session,
                        username=username,
                        run_id=run_id,
                        runs=_runs,
                    )
                except Exception as e:
                    logger.error("Error scraping session %s for %s: %s", session_id, username, e)
    except Exception as e:
        logger.error("Daily job failed: %s", e)
