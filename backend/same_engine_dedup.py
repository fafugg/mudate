"""Same-engine deduplication (reactivation detection).

Detects cases where a real estate agent deactivated a listing and re-created
it as "new" to bump it to the top.  Finds pairs of houses from the same
search engine where one was removed and one was newly added, matching on
address similarity, price tolerance, and square-meter tolerance.
"""

import logging
from typing import Any, Dict, List, Optional

from storage import _now, atomic_update, read_db
from deduplicator import (
    _address_similarity,
    _within_tolerance,
    _SIMILARITY_THRESHOLD,
    _PRICE_TOLERANCE,
    _M2_TOLERANCE,
)

logger = logging.getLogger(__name__)


# ── Detection ─────────────────────────────────────────────────────────────────

def find_same_engine_duplicates(
    session: dict,
    username: str,
    run_started_at: Optional[str] = None,
) -> List[dict]:
    """Find pairs of houses from the same engine where one was removed and one was added.

    Args:
        session: The session dict from db.json.
        username: The owning user.
        run_started_at: ISO timestamp of when the scrape run started.
            Used to identify houses that were removed or added during this run.
            If None, uses all removed/active houses (for manual preview).

    Returns:
        List of groups, each containing:
        - old_id: internal_id of the removed house (to keep)
        - new_id: internal_id of the new house (to delete)
        - engine: the search engine
        - old_house: summary dict of the old house
        - new_house: summary dict of the new house
    """
    db = read_db()
    house_ids = session.get("house_ids", [])

    removed_houses = []
    new_houses = []

    for hid in house_ids:
        h = db["houses"].get(hid)
        if not h:
            continue

        if run_started_at:
            # Only consider houses changed during this scrape run
            if h.get("status") == "removed" and h.get("removed_at", "") >= run_started_at:
                removed_houses.append((hid, h))
            elif h.get("status") == "active" and h.get("created_at", "") >= run_started_at:
                new_houses.append((hid, h))
        else:
            # Manual mode: consider all removed and active houses
            if h.get("status") == "removed":
                removed_houses.append((hid, h))
            elif h.get("status") == "active":
                new_houses.append((hid, h))

    # Group by search engine
    removed_by_engine: Dict[str, List[tuple]] = {}
    for hid, h in removed_houses:
        engine = h.get("search_engine", "")
        removed_by_engine.setdefault(engine, []).append((hid, h))

    new_by_engine: Dict[str, List[tuple]] = {}
    for hid, h in new_houses:
        engine = h.get("search_engine", "")
        new_by_engine.setdefault(engine, []).append((hid, h))

    # Find matching pairs within each engine
    groups = []
    for engine in set(removed_by_engine.keys()) & set(new_by_engine.keys()):
        engine_removed = removed_by_engine[engine]
        engine_new = new_by_engine[engine]

        # Build match graph
        n_removed = len(engine_removed)
        n_new = len(engine_new)
        matched_pairs: List[tuple] = []

        for ri, (rh_id, rh) in enumerate(engine_removed):
            addr_r = rh.get("manual_address") or rh.get("address") or ""
            price_r = rh.get("price")
            m2_r = rh.get("total_m2") or rh.get("covered_m2")

            for ni, (nh_id, nh) in enumerate(engine_new):
                addr_n = nh.get("manual_address") or nh.get("address") or ""
                price_n = nh.get("price")
                m2_n = nh.get("total_m2") or nh.get("covered_m2")

                if not addr_r or not addr_n:
                    continue

                if _address_similarity(addr_r, addr_n) < _SIMILARITY_THRESHOLD:
                    continue

                if not _within_tolerance(price_r, price_n, _PRICE_TOLERANCE):
                    continue

                if not _within_tolerance(m2_r, m2_n, _M2_TOLERANCE):
                    continue

                matched_pairs.append((ri, ni, rh_id, nh_id))

        # Build groups: each removed house can match multiple new houses
        # Group by removed house index
        removed_to_new: Dict[int, List[tuple]] = {}
        for ri, ni, rh_id, nh_id in matched_pairs:
            removed_to_new.setdefault(ri, []).append((ni, rh_id, nh_id))

        for ri, matches in removed_to_new.items():
            rh_id, rh = engine_removed[ri]
            for ni, rh_id, nh_id in matches:
                nh = db["houses"].get(nh_id, {})
                groups.append({
                    "old_id": rh_id,
                    "new_id": nh_id,
                    "engine": engine,
                    "old_house": _summarize(rh),
                    "new_house": _summarize(nh),
                })

    return groups


def _summarize(h: dict) -> dict:
    """Return a lightweight summary of a house for the API response."""
    return {
        "internal_id": h.get("internal_id", ""),
        "search_engine": h.get("search_engine", ""),
        "search_engine_id": h.get("search_engine_id", ""),
        "address": h.get("manual_address") or h.get("address") or "",
        "price": h.get("price"),
        "currency": h.get("currency", "USD"),
        "total_m2": h.get("total_m2"),
        "covered_m2": h.get("covered_m2"),
        "review": h.get("review"),
        "url": h.get("url", ""),
    }


# ── Apply ─────────────────────────────────────────────────────────────────────

# Mutable fields to sync from the new listing to the old house
_SYNC_FIELDS = [
    "type", "ambientes", "dormitorios", "banos", "toilettes",
    "price", "currency", "price_per_m2", "expenses", "expenses_currency",
    "address", "covered_m2", "total_m2", "floor", "parking", "amenities",
    "orientation", "age_years", "condition",
    "real_estate", "real_estate_phone", "published_at",
    "images", "description",
]


def apply_same_engine_dedup(
    session: dict,
    username: str,
    groups: List[dict],
) -> int:
    """Merge selected same-engine duplicate groups.

    For each group:
    - Updates the old house with the new house's mutable fields
    - Records price change in previous_prices if applicable
    - Transfers the new house's search_engine_id to the old house
    - Deletes the new house from the database
    - Removes the new house from the session's house_ids

    Returns the number of houses merged.
    """
    now = _now()
    merged_count = 0
    session_id = session["id"]

    def update(db: dict):
        nonlocal merged_count

        for group in groups:
            old_id = group["old_id"]
            new_id = group["new_id"]

            old_house = db["houses"].get(old_id)
            new_house = db["houses"].get(new_id)
            if not old_house or not new_house:
                continue

            # Sync mutable fields from new → old
            new_price = new_house.get("price")
            old_price = old_house.get("price")

            if new_price and old_price and new_price != old_price:
                old_house.setdefault("previous_prices", []).append({
                    "price": old_price,
                    "currency": old_house.get("currency", "USD"),
                    "timestamp": old_house.get("last_updated", now),
                })

            for field in _SYNC_FIELDS:
                if new_house.get(field) is not None:
                    old_house[field] = new_house[field]

            # Transfer search_engine_id so future scrapes match the old house
            old_house["search_engine_id"] = new_house.get("search_engine_id")
            old_house["last_updated"] = now

            # Delete the new house
            del db["houses"][new_id]

            # Remove new_id from session's house_ids
            hids = db["users"][username]["sessions"][session_id].get("house_ids", [])
            db["users"][username]["sessions"][session_id]["house_ids"] = [
                hid for hid in hids if hid != new_id
            ]

            merged_count += 1

    atomic_update(update)
    return merged_count
