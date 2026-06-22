import uuid
from typing import Any, Dict, List, Optional

from storage import atomic_update, _now, read_db

from .argenprop import ArgenpropScraper
from .zonaprop import ZonapropScraper


def get_scraper(engine: str):
    if engine == "zonaprop":
        return ZonapropScraper()
    if engine == "argenprop":
        return ArgenpropScraper()
    raise ValueError(f"Unknown engine: {engine}")


def _make_run(
    run_id: str,
    *,
    session_id: Optional[str] = None,
    total: int = 0,
    message: str = "Iniciando...",
    triggered_by: str = "manual",
) -> dict:
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


def _mark_cancelled(runs: Dict[str, dict], run_id: str) -> None:
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
        engine  = session["search_engine"]

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
            _mark_cancelled(runs, run_id)
            return

        runs[run_id].update(
            {
                "message": f"Guardando {len(listings)} propiedades...",
                "total": len(listings),
            }
        )

        _persist_listings(listings, session, username)

        if should_cancel():
            _mark_cancelled(runs, run_id)
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


def _persist_listings(
    listings: List[Dict[str, Any]],
    session: dict,
    username: str,
) -> None:
    now = _now()
    session_id = session["id"]
    engine = session["search_engine"]

    def update(db: dict):
        # ── Build lookup index ────────────────────────────────────────────────
        # Search across ALL of the user's houses for this search engine, not
        # just the current session.  This means a property already scraped (and
        # geocoded / reviewed) in another session is reused rather than
        # duplicated — preserving lat/lng, notes, and price history.
        by_se_id: Dict[str, str] = {}  # search_engine_id → hid
        by_url: Dict[str, str] = {}    # url → hid

        # Pre-build a set of hids owned by this user for O(1) lookup.
        user_hids: set = set()
        for s in db["users"].get(username, {}).get("sessions", {}).values():
            user_hids.update(s.get("house_ids", []))

        for hid in user_hids:
            house = db["houses"].get(hid)
            if not house or house.get("search_engine") != engine:
                continue
            if house.get("search_engine_id"):
                by_se_id[house["search_engine_id"]] = hid
            if house.get("url"):
                by_url[house["url"]] = hid

        # The "removed" check only applies to houses that were previously part
        # of THIS session — not every house the user has ever scraped.
        current_session_hids: set = set(
            db["users"][username]["sessions"][session_id].get("house_ids", [])
        )

        new_ids: List[str] = []
        matched_hids: set = set()

        for listing in listings:
            url = listing.get("url", "")
            if not url:
                continue

            se_id = listing.get("search_engine_id", "")

            # Prefer search_engine_id match, fall back to URL match
            hid = (by_se_id.get(se_id) if se_id else None) or by_url.get(url)

            if hid:
                house = db["houses"][hid]
                new_price = listing.get("price")
                if new_price and new_price != house.get("price"):
                    house.setdefault("previous_prices", []).append(
                        {
                            "price": house.get("price"),
                            "currency": house.get("currency", "USD"),
                            "timestamp": house.get("last_updated", now),
                        }
                    )
                _merge(house, listing, now)
                house["status"] = "active"
                house["removed_at"] = None
                matched_hids.add(hid)
                new_ids.append(hid)
            else:
                hid = str(uuid.uuid4())
                db["houses"][hid] = _new_house(hid, listing, session_id, engine, now)
                matched_hids.add(hid)
                new_ids.append(hid)

        # Houses that were in THIS session but not seen in this run → removed.
        # We deliberately do NOT touch houses from other sessions here.
        for hid in current_session_hids:
            if hid not in matched_hids and hid in db["houses"]:
                db["houses"][hid]["status"] = "removed"
                db["houses"][hid]["removed_at"] = now
                new_ids.append(hid)

        db["users"][username]["sessions"][session_id]["last_executed"] = now
        # Deduplicate while preserving order (matched first, removed last)
        seen: set = set()
        unique_ids: List[str] = []
        for hid in new_ids:
            if hid not in seen:
                seen.add(hid)
                unique_ids.append(hid)
        db["users"][username]["sessions"][session_id]["house_ids"] = unique_ids

    atomic_update(update)


def _merge(house: dict, listing: dict, now: str) -> None:
    updatable = [
        "type", "ambientes", "dormitorios", "banos", "toilettes", "price", "currency",
        "price_per_m2", "expenses", "expenses_currency", "address",
        "covered_m2", "total_m2", "floor", "parking", "amenities",
        "orientation", "age_years", "condition", "real_estate", "real_estate_phone",
        "published_at", "images", "description",
    ]
    for field in updatable:
        if listing.get(field) is not None:
            house[field] = listing[field]
    house["last_updated"] = now


def _new_house(hid: str, listing: dict, session_id: str, engine: str, now: str) -> dict:
    return {
        "internal_id": hid,
        "search_engine_id": listing.get("search_engine_id"),
        "search_engine": engine,
        "session_id": session_id,
        "type": listing.get("type"),
        "ambientes": listing.get("ambientes"),
        "dormitorios": listing.get("dormitorios"),
        "banos": listing.get("banos"),
        "toilettes": listing.get("toilettes"),
        "price": listing.get("price"),
        "currency": listing.get("currency", "USD"),
        "price_per_m2": listing.get("price_per_m2"),
        "expenses": listing.get("expenses"),
        "expenses_currency": listing.get("expenses_currency"),
        "address": listing.get("address"),
        "covered_m2": listing.get("covered_m2"),
        "total_m2": listing.get("total_m2"),
        "floor": listing.get("floor"),
        "parking": listing.get("parking"),
        "amenities": listing.get("amenities", []),
        "orientation": listing.get("orientation"),
        "age_years": listing.get("age_years"),
        "condition": listing.get("condition"),
        "real_estate": listing.get("real_estate"),
        "real_estate_phone": listing.get("real_estate_phone"),
        "published_at": listing.get("published_at"),
        "images": listing.get("images", []),
        "description": listing.get("description"),
        "created_at": now,
        "last_updated": now,
        "previous_prices": [],
        "url": listing.get("url", ""),
        "status": "active",
        "removed_at": None,
        "review": None,
        "notes": None,
        "manual_address": None,
        "lat": None,
        "lng": None,
        "geocode_failed": False,
    }


