"""Database persistence for scraped listings.

Handles merging new listings into the database, deduplication across sessions,
price history tracking, and marking removed properties.
"""

import uuid
from typing import Any, Dict, List

from storage import atomic_update, _now


def persist_listings(
    listings: List[Dict[str, Any]],
    session: dict,
    username: str,
) -> None:
    """Merge scraped listings into the database.

    For each listing:
    - If a house with the same search_engine_id or URL exists in ANY of the
      user's sessions, update it in-place (preserving review, notes, geocoords).
    - If it's new, create a fresh house record.
    - Houses that were in this session but absent from the current scrape
      are marked as "removed".
    """
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
    """Update a house record's mutable fields from a fresh listing.

    Does NOT touch user-set fields: review, notes, manual_address, lat, lng.
    """
    updatable = [
        "type", "ambientes", "dormitorios", "banos", "toilettes", "price", "currency",
        "price_per_m2", "expenses", "expenses_currency", "address",
        "covered_m2", "total_m2", "floor", "parking", "amenities",
        "orientation", "age_years", "condition", "real_estate", "real_estate_phone",
        "published_at", "images", "description", "lat", "lng",
    ]
    for field in updatable:
        if listing.get(field) is not None:
            house[field] = listing[field]
    house["last_updated"] = now


def _new_house(hid: str, listing: dict, session_id: str, engine: str, now: str) -> dict:
    """Create a new house record from a scraped listing."""
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
        "lat": listing.get("lat"),
        "lng": listing.get("lng"),
        "geocode_failed": False,
    }
