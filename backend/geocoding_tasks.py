"""Geocoding background tasks.

Handles concurrent geocoding of house addresses with rate limiting.
Extracted from main.py for cleaner separation of concerns.
"""

import asyncio
from typing import Dict, List, Tuple

from config import settings
from geocoder import geocode, save_geocache
from storage import _now, atomic_update, read_db

# Flush pending results to db.json every N geocoded houses. The old code did
# read_db() + atomic_update() per house: two full 6.4 MB parses and one full
# rewrite each (≈28 s of JSON overhead for a 184-house run, all serialized
# through the global file lock). Batching keeps progress persisted while
# turning 184 writes into ~19.
_FLUSH_EVERY = 10


async def run_geocode(house_ids: List[str], run_id: str, runs: Dict, force: bool = False) -> None:
    """Geocode a list of houses concurrently.

    Up to settings.geocode_batch_size houses run at the same time. Within each
    call, geocoder.py handles rate limiting (Nominatim 1 req/s via pipelined
    semaphore) and consults the persistent address cache before hitting the
    network. Results are written to db.json in batches of _FLUSH_EVERY.
    """

    total = len(house_ids)
    completed = 0
    sem = asyncio.Semaphore(settings.geocode_batch_size)
    lock = asyncio.Lock()  # guards `completed` counter and run status updates

    # Read the DB ONCE for the whole run (address lookups).
    db = read_db()
    addresses: Dict[str, str] = {}
    for hid in house_ids:
        house = db["houses"].get(hid) or {}
        addr = house.get("manual_address") or house.get("address")
        if addr:
            addresses[hid] = addr

    pending: Dict[str, Tuple[float, float]] = {}  # hid -> (lat, lng)
    run = runs[run_id]
    run.setdefault("updated_houses", [])  # delta for the frontend poll

    def _flush() -> None:
        """Write every result completed so far in ONE atomic_update."""
        if not pending:
            return
        batch = dict(pending)

        def _save(db: dict) -> None:
            for hid, (lat, lng) in batch.items():
                house = db["houses"].get(hid)
                if house is not None:
                    house["lat"] = lat
                    house["lng"] = lng
                    house["geocode_failed"] = lat is None

        atomic_update(_save)
        pending.clear()  # only after the write succeeded
        save_geocache()  # same cadence for the address cache: bounded, crash-safe
        # Record what changed so GET /api/runs/{id} can serve a mergeable
        # delta instead of the frontend refetching the whole session.
        run["updated_houses"].extend(
            {"internal_id": hid, "lat": lat, "lng": lng, "geocode_failed": lat is None}
            for hid, (lat, lng) in batch.items()
        )

    async def _one(hid: str) -> None:
        nonlocal completed
        async with sem:
            if runs[run_id].get("cancelled"):
                return

            address = addresses.get(hid)
            if address:
                lat, lng = await geocode(address, force=force)
                # Safe without the lock: the event loop is single-threaded and
                # there is no await between these two statements.
                pending[hid] = (lat, lng)

            async with lock:
                completed += 1
                runs[run_id].update({
                    "progress": completed,
                    "message": f"Geocodificando {completed}/{total}…",
                })
                if len(pending) >= _FLUSH_EVERY:
                    _flush()

    await asyncio.gather(*[_one(hid) for hid in house_ids])

    _flush()         # persist whatever is left (also on cancel)
    save_geocache()  # no-op if the last flush already wrote the address cache

    if runs[run_id].get("cancelled"):
        runs[run_id].update({"status": "cancelled", "message": "Cancelado.", "finished_at": _now()})
    else:
        runs[run_id].update({
            "status": "done",
            "message": f"Listo. {total} direcciones procesadas.",
            "progress": total,
            "finished_at": _now(),
        })
