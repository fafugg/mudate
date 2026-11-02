"""Geocoding background tasks.

Handles concurrent geocoding of house addresses with rate limiting.
Extracted from main.py for cleaner separation of concerns.
"""

import asyncio
import logging
from typing import Dict, List

from config import settings
from geocoder import GeocodeResult, geocode, save_geocache
from storage import _now, atomic_update, read_db

# Flush pending results to db.json every N geocoded houses. The old code did
# read_db() + atomic_update() per house: two full 6.4 MB parses and one full
# rewrite each (≈28 s of JSON overhead for a 184-house run, all serialized
# through the global file lock). Batching keeps progress persisted while
# turning 184 writes into ~19.
_FLUSH_EVERY = 10
logger = logging.getLogger(__name__)


async def run_geocode(house_ids: List[str], run_id: str, runs: Dict, force: bool = False) -> None:
    """Geocode a list of houses concurrently.

    Up to settings.geocode_batch_size houses run at the same time. Within each
    call, geocoder.py handles provider limits and consults the persistent address cache before hitting the
    network. Results are written to db.json in batches of _FLUSH_EVERY.
    """

    total = len(house_ids)
    completed = 0
    temporary_errors = 0
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

    pending: Dict[str, GeocodeResult] = {}
    run = runs[run_id]
    run.setdefault("updated_houses", [])  # delta for the frontend poll

    def _flush() -> None:
        """Write every result completed so far in ONE atomic_update."""
        if not pending:
            return
        batch = dict(pending)

        applied = []

        def _save(db: dict) -> None:
            for hid, result in batch.items():
                house = db["houses"].get(hid)
                if house is None or (house.get("manual_address") or house.get("address")) != addresses.get(hid):
                    continue  # Address was edited or the house was deleted during the request.
                if house.get("lat") is not None and house.get("lng") is not None:
                    continue  # Another run or a manual change already located it.
                house["lat"] = result.lat
                house["lng"] = result.lng
                house["geocode_failed"] = result.lat is None and not result.temporary_error
                house["geocode_provider"] = result.provider
                house["geocode_precision"] = result.precision
                house["geocode_attribution"] = result.source_attribution
                house["geocode_attribution_url"] = result.source_url
                applied.append({"internal_id": hid, "lat": result.lat, "lng": result.lng,
                                "geocode_failed": house["geocode_failed"],
                                "geocode_provider": result.provider, "geocode_precision": result.precision,
                                "geocode_attribution": result.source_attribution,
                                "geocode_attribution_url": result.source_url})

        atomic_update(_save)
        pending.clear()  # only after the write succeeded
        save_geocache()  # same cadence for the address cache: bounded, crash-safe
        # Record what changed so GET /api/runs/{id} can serve a mergeable
        # delta instead of the frontend refetching the whole session.
        run["updated_houses"].extend(applied)

    async def _one(hid: str) -> None:
        nonlocal completed, temporary_errors
        async with sem:
            if runs[run_id].get("cancelled"):
                return

            address = addresses.get(hid)
            if address:
                try:
                    result = await geocode(address, force=force)
                except Exception:
                    logger.exception("Unexpected geocoding failure")
                    result = GeocodeResult(temporary_error=True)
                if result.temporary_error:
                    temporary_errors += 1
                # Safe without the lock: the event loop is single-threaded and
                # there is no await between these two statements.
                pending[hid] = result

            async with lock:
                completed += 1
                runs[run_id].update({
                    "progress": completed,
                    "message": f"Geocodificando {completed}/{total}…",
                })
                if len(pending) >= _FLUSH_EVERY:
                    _flush()

    tasks = [asyncio.create_task(_one(hid)) for hid in house_ids]
    try:
        await asyncio.gather(*tasks)
        _flush()         # persist whatever is left (also on cancel)
        save_geocache()
        if runs[run_id].get("cancelled"):
            runs[run_id].update({"status": "cancelled", "message": "Cancelado.", "finished_at": _now()})
        else:
            message = f"Listo. {total} direcciones procesadas."
            if temporary_errors:
                message += f" {temporary_errors} con errores temporales; se pueden reintentar."
            runs[run_id].update({
                "status": "partial" if temporary_errors else "done",
                "message": message, "progress": total, "finished_at": _now(),
            })
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.exception("Geocoding run failed")
        runs[run_id].update({"status": "error", "message": "Error al guardar la geocodificación.", "finished_at": _now()})
