"""Single source of truth for house-record field rules.

Every module that syncs, merges, or records a publication date / price on a
house must go through these helpers — the logic lives here and nowhere else.

Invariant: ``first_published_at`` and ``republish_count`` are deliberately NOT
part of :data:`MUTABLE_LISTING_FIELDS`.  A scraped listing therefore can never
overwrite them through :func:`sync_mutable_fields`; the original publication
date only ever moves *earlier*, through :func:`advance_first_published`.

``published_at`` is the *current* posting date (refreshed on every scrape),
while ``first_published_at`` is the *original* one and is stable forever.
"""

import logging
from typing import Any, Dict, Optional

from storage import atomic_update

logger = logging.getLogger(__name__)


# Fields copied from a scraped listing (or a newer record) onto an existing
# house.  ``published_at`` IS here because it tracks the current posting;
# ``first_published_at`` / ``republish_count`` are NOT — see module docstring.
MUTABLE_LISTING_FIELDS = [
    "type", "ambientes", "dormitorios", "banos", "toilettes",
    "price", "currency", "price_per_m2", "expenses", "expenses_currency",
    "address", "covered_m2", "total_m2", "floor", "parking", "amenities",
    "orientation", "age_years", "condition",
    "real_estate", "real_estate_phone", "published_at",
    "images", "description",
]

# Persisted publish-tracking fields.  Never overwritten by a scrape or merge.
PUBLISH_FIELDS = ("first_published_at", "republish_count")


def date_only(value: Optional[str]) -> Optional[str]:
    """Normalize an ISO timestamp/date string to ``YYYY-MM-DD`` (best effort)."""
    if not value:
        return None
    return str(value)[:10] or None


def earliest_date(*dates: Optional[str]) -> Optional[str]:
    """Return the earliest non-empty date, normalized to ``YYYY-MM-DD``."""
    clean = [d for d in (date_only(v) for v in dates) if d]
    return min(clean) if clean else None


def original_published_at(house: Dict[str, Any]) -> Optional[str]:
    """Best-known original publication date for a house.

    Falls back to the current posting date, then to when we first saw it.
    """
    return (
        date_only(house.get("first_published_at"))
        or date_only(house.get("published_at"))
        or date_only(house.get("created_at"))
    )


def backfill_publish_fields(house: Dict[str, Any]) -> None:
    """Ensure the publish-tracking fields exist. Idempotent.

    Used both when a house is created and when migrating legacy records.
    """
    if not house.get("first_published_at"):
        house["first_published_at"] = (
            date_only(house.get("published_at"))
            or date_only(house.get("created_at"))
        )
    house.setdefault("republish_count", 0)


def advance_first_published(house: Dict[str, Any], published_at: Optional[str]) -> None:
    """Move ``first_published_at`` earlier if ``published_at`` predates it.

    It never moves later, so re-scrapes and re-posts cannot overwrite the
    original publication date.
    """
    house["first_published_at"] = earliest_date(
        original_published_at(house), published_at
    )


def bump_republish(house: Dict[str, Any], by: int = 1) -> None:
    """Increment the re-post counter, initializing it when missing."""
    house["republish_count"] = int(house.get("republish_count") or 0) + by


def remember_price(house: Dict[str, Any], new_price: Optional[float], now: str) -> None:
    """Record the house's current price in history when a new price differs."""
    old_price = house.get("price")
    if new_price and old_price and new_price != old_price:
        house.setdefault("previous_prices", []).append({
            "price": old_price,
            "currency": house.get("currency", "USD"),
            "timestamp": house.get("last_updated") or now,
        })


def sync_mutable_fields(house: Dict[str, Any], source: Dict[str, Any]) -> None:
    """Copy non-null mutable listing fields from ``source`` onto ``house``.

    Does not touch user-set fields (review, notes, manual_address, lat, lng)
    nor the publish-tracking fields.
    """
    for field in MUTABLE_LISTING_FIELDS:
        if source.get(field) is not None:
            house[field] = source[field]


def migrate_publish_fields() -> int:
    """Backfill publish-tracking fields on every legacy house. Idempotent.

    Returns the number of houses that were updated.
    """
    updated = 0

    def update(db: dict) -> None:
        nonlocal updated
        for house in db.get("houses", {}).values():
            before = (house.get("first_published_at"), house.get("republish_count"))
            backfill_publish_fields(house)
            if (house.get("first_published_at"), house.get("republish_count")) != before:
                updated += 1

    atomic_update(update)
    if updated:
        logger.info("Backfilled publish fields on %d houses", updated)
    return updated
