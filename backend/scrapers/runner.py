"""Scrape run orchestration.

Manages the lifecycle of a scrape run: start, progress updates, cancellation,
and completion. Works with the in-memory runs dict for status tracking.
"""

from typing import Any, Dict, List, Optional

from storage import _now, read_db
from .factory import get_scraper
from .persistence import persist_listings


def make_run(
    run_id: str,
    *,
    session_id: Optional[str] = None,
    total: int = 0,
    message: str = "Iniciando...",
    triggered_by: str = "manual",
) -> dict:
    """Create a new run status dict."""
    return {
        "id": run_id,
        "session_id": session_id,
        "status": "running",
        "progress": 0,
        "total": total,
        "message": message,
        "started_at": _now(),
        "finished_at": None,
        "errors": [],
        "triggered_by": triggered_by,
    }


def mark_cancelled(runs: Dict[str, dict], run_id: str) -> None:
    """Mark a run as cancelled by the user."""
    runs[run_id].update({
        "status": "cancelled",
        "message": "Cancelado por el usuario.",
        "finished_at": _now(),
    })


async def run_scrape(
    session: dict,
    username: str,
    run_id: str,
    runs: Dict[str, dict],
) -> None:
    """Background task: scrape all listings for a session and update db.json."""

    def progress(msg: str, current: int, total: int):
        runs[run_id]["message"] = msg
        runs[run_id]["progress"] = current
        runs[run_id]["total"] = total

    try:
        scraper = get_scraper(session["search_engine"])
        engine = session["search_engine"]

        def should_cancel() -> bool:
            return runs[run_id].get("cancelled", False)

        # Build the set of search_engine_ids already in the DB for this
        # user+engine so scrapers can skip detail pages for known houses.
        db = read_db()
        user_hids: set = set()
        for s in db.get("users", {}).get(username, {}).get("sessions", {}).values():
            user_hids.update(s.get("house_ids", []))
        existing_ids: set = {
            house["search_engine_id"]
            for hid in user_hids
            if (house := db["houses"].get(hid))
            and house.get("search_engine") == engine
            and house.get("search_engine_id")
        }

        listings: List[Dict[str, Any]] = await scraper.scrape_search(
            search_filter=session["search_filter"],
            progress_callback=progress,
            cancel_check=should_cancel,
            existing_ids=existing_ids,
        )

        if should_cancel():
            mark_cancelled(runs, run_id)
            return

        runs[run_id].update(
            {
                "message": f"Guardando {len(listings)} propiedades...",
                "total": len(listings),
            }
        )

        persist_listings(listings, session, username)

        if should_cancel():
            mark_cancelled(runs, run_id)
            return

        runs[run_id].update(
            {
                "status": "done",
                "message": f"Listo. {len(listings)} propiedades procesadas.",
                "progress": len(listings),
                "total": len(listings),
                "finished_at": _now(),
            }
        )

    except Exception as exc:
        runs[run_id].update(
            {
                "status": "error",
                "message": f"Error: {exc}",
                "finished_at": _now(),
                "errors": runs[run_id].get("errors", []) + [str(exc)],
            }
        )
