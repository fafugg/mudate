"""Geocoding background tasks.

Handles concurrent geocoding of house addresses with rate limiting.
Extracted from main.py for cleaner separation of concerns.
"""

import asyncio
from typing import Dict, List

from config import settings
from storage import _now, atomic_update, read_db


async def run_geocode(house_ids: List[str], run_id: str, runs: Dict) -> None:
    """Geocode a list of houses concurrently.

    Up to settings.geocode_batch_size houses run at the same time. Within each
    call, geocoder.py handles rate limiting (Nominatim 1 req/s via pipelined
    semaphore). The per-house sleep is gone — rate limiting lives entirely in
    the geocoder module.
    """
    from geocoder import geocode

    total = len(house_ids)
    completed = 0
    sem = asyncio.Semaphore(settings.geocode_batch_size)
    lock = asyncio.Lock()  # guards `completed` counter

    async def _one(hid: str) -> None:
        nonlocal completed
        async with sem:
            if runs[run_id].get("cancelled"):
                return

            db = read_db()
            house = db["houses"].get(hid, {})
            address = house.get("manual_address") or house.get("address")

            if address:
                lat, lng = await geocode(address)

                def _save(db: dict, _hid=hid, _lat=lat, _lng=lng):
                    if _hid in db["houses"]:
                        db["houses"][_hid]["lat"] = _lat
                        db["houses"][_hid]["lng"] = _lng
                        db["houses"][_hid]["geocode_failed"] = _lat is None

                atomic_update(_save)

            async with lock:
                completed += 1
                runs[run_id].update({
                    "progress": completed,
                    "message": f"Geocodificando {completed}/{total}…",
                })

    await asyncio.gather(*[_one(hid) for hid in house_ids])

    if runs[run_id].get("cancelled"):
        runs[run_id].update({"status": "cancelled", "message": "Cancelado.", "finished_at": _now()})
    else:
        runs[run_id].update({
            "status": "done",
            "message": f"Listo. {total} direcciones procesadas.",
            "progress": total,
            "finished_at": _now(),
        })
