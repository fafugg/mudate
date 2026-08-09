"""Scrape run orchestration.

Manages the lifecycle of a scrape run: start, progress updates, cancellation,
and completion. Works with the in-memory runs dict for status tracking.
"""

import asyncio
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
    """Background task: scrape all listings for a session and update db.json.

    Supports multiple search_sources — runs each in parallel.
    """

    def progress(msg: str, current: int, total: int):
        runs[run_id]["message"] = msg
        runs[run_id]["progress"] = current
        runs[run_id]["total"] = total

    try:
        sources = session.get("search_sources", [])

        def should_cancel() -> bool:
            return runs[run_id].get("cancelled", False)

        # Build existing_ids per engine for all user's houses
        db = read_db()
        user_hids: set = set()
        for s in db.get("users", {}).get(username, {}).get("sessions", {}).values():
            user_hids.update(s.get("house_ids", []))

        # Run all sources sequentially
        async def _scrape_one(source: dict) -> List[Dict[str, Any]]:
            engine = source["engine"]
            scraper = get_scraper(engine)
            existing_ids = {
                house["search_engine_id"]
                for hid in user_hids
                if (house := db["houses"].get(hid))
                and house.get("search_engine") == engine
                and house.get("search_engine_id")
            }
            listings = await scraper.scrape_search(
                search_filter=source["filter"],
                progress_callback=progress,
                cancel_check=should_cancel,
                existing_ids=existing_ids,
            )
            # Check for partial scrape results
            paging_info = getattr(scraper, "last_paging_info", None)
            if paging_info:
                expected = paging_info.get("total", 0)
                if expected and len(listings) < expected * 0.5:
                    runs[run_id]["errors"].append(
                        f"{engine}: resultados parciales ({len(listings)}/{expected})"
                    )
            # Tag each listing with its engine so persist_listings uses the correct one
            for listing in listings:
                listing["search_engine"] = engine
            return listings

        # Multiple sources — sequential (avoids Playwright resource conflicts)
        all_listings: list = []
        source_errors: list = []
        for i, source in enumerate(sources):
            if should_cancel():
                break
            engine = source["engine"]
            progress(f"Scraping {engine} ({i+1}/{len(sources)})...", len(all_listings), len(all_listings))
            try:
                listings = await _scrape_one(source)
                all_listings.extend(listings)
                progress(f"{engine}: {len(listings)} propiedades", len(all_listings), len(all_listings))
            except Exception as e:
                source_errors.append(f"{engine}: {e}")
                runs[run_id]["errors"].append(f"{engine}: {e}")
                logger.error("Scrape error %s: %s", engine, e)

        if should_cancel():
            mark_cancelled(runs, run_id)
            return

        runs[run_id].update(
            {
                "message": f"Guardando {len(all_listings)} propiedades...",
                "total": len(all_listings),
            }
        )

        persist_listings(all_listings, session, username)

        if should_cancel():
            mark_cancelled(runs, run_id)
            return

        has_partial = len(source_errors) > 0 and len(all_listings) > 0
        has_error = len(source_errors) > 0 and len(all_listings) == 0

        if has_error:
            status = "error"
            message = f"Error: {source_errors[0]}"
        elif has_partial:
            status = "partial"
            failed = ", ".join(source_errors)
            message = f"Listo. {len(all_listings)} propiedades procesadas. Falló: {failed}"
        else:
            status = "done"
            message = f"Listo. {len(all_listings)} propiedades procesadas."

        runs[run_id].update(
            {
                "status": status,
                "message": message,
                "progress": len(all_listings),
                "total": len(all_listings),
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


import logging
logger = logging.getLogger(__name__)
