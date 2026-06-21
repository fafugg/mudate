import asyncio
import uuid
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from scrapers import run_scrape
from storage import read_db

_scheduler = AsyncIOScheduler(timezone="America/Argentina/Buenos_Aires")
_runs: dict = {}  # shared with main.py after init


def setup_scheduler(shared_runs: dict) -> None:
    global _runs
    _runs = shared_runs
    _scheduler.add_job(
        _daily_job,
        CronTrigger(hour=8, minute=0),
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
                _runs[run_id] = {
                    "id": run_id,
                    "session_id": session_id,
                    "status": "running",
                    "progress": 0,
                    "total": 0,
                    "message": "Actualización diaria...",
                    "started_at": datetime.utcnow().isoformat() + "Z",
                    "finished_at": None,
                    "errors": [],
                    "triggered_by": "scheduler",
                }
                try:
                    await run_scrape(
                        session=session,
                        username=username,
                        run_id=run_id,
                        runs=_runs,
                    )
                except Exception as e:
                    print(f"[SCHEDULER] Error scraping session {session_id} for {username}: {e}")
    except Exception as e:
        print(f"[SCHEDULER] Daily job failed: {e}")
