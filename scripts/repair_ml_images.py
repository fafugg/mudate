#!/usr/bin/env python3
"""Re-fetch MercadoLibre detail pages to restore clobbered image galleries.

Background: a house's gallery comes from its detail page (~5 photos), but
known listings skip the detail page on every run and the search card only
carries a single thumbnail.  persistence used to copy that thumbnail over
the stored gallery, so each daily run stripped every known listing back to
1 image — 359 active ML houses ended up that way.

The merge rule is fixed (sync_mutable_fields never shrinks the gallery);
this script repairs the historical records by re-visiting each affected
detail page and re-collecting its images.

Only images are touched; price, surfaces, review, notes, geo-coordinates
and price history are never modified.  A detail page that yields no more
images than already stored leaves the record untouched, so a run can never
degrade a record.

Usage (from anywhere):
    python scripts/repair_ml_images.py --dry-run              # preview targets
    python scripts/repair_ml_images.py                        # repair 1-image houses
    python scripts/repair_ml_images.py --max-images 3         # also houses with ≤3
    python scripts/repair_ml_images.py --limit 5              # first 5 only
    python scripts/repair_ml_images.py --batch 50             # batch writes
    python scripts/repair_ml_images.py --concurrency 3        # parallel tabs
    python scripts/repair_ml_images.py --delay 0.5            # pace the requests
    python scripts/repair_ml_images.py --include-removed      # also removed
    python scripts/repair_ml_images.py --engine-id MLA-123    # single listing (repeatable)
"""

import os
import sys

# ── Auto-detect and re-exec with project venv ────────────────────────────
_THIS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PREFIX = os.path.join(_THIS, "backend", ".venv")
_VENV_PYTHON = os.path.join(_VENV_PREFIX, "bin", "python")


def _is_venv() -> bool:
    return os.path.realpath(sys.prefix) == os.path.realpath(_VENV_PREFIX)


if not _is_venv() and os.path.isfile(_VENV_PYTHON):
    os.execv(_VENV_PYTHON, [_VENV_PYTHON] + sys.argv)

import argparse
import asyncio
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.join(_THIS, "backend"))

from playwright.async_api import async_playwright
from config import settings
from scrapers.mercadolibre import _scrape_detail
from storage import _now, atomic_update, read_db

UA = settings.user_agent
_INIT_SCRIPT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


def _needs_repair(h: dict, max_images: int) -> bool:
    """True when the stored gallery is smaller than a detail page produces."""
    return len(h.get("images") or []) <= max_images


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair MercadoLibre image galleries from detail pages"
    )
    parser.add_argument("--limit", type=int, default=0, help="Only process N houses")
    parser.add_argument("--dry-run", action="store_true", help="Show targets without writing")
    parser.add_argument("--batch", type=int, default=50, help="Write updates in batches of N (default: 50)")
    parser.add_argument("--include-removed", action="store_true", help="Include houses with status=removed")
    parser.add_argument("--concurrency", type=int, default=3, help="Number of concurrent tabs (default: 3)")
    parser.add_argument("--max-images", type=int, default=1,
                        help="Repair houses with at most this many images (default: 1)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to wait before each detail fetch, to stay under ML's rate limit")
    parser.add_argument("--engine-id", action="append", default=[], metavar="MLA-123",
                        help="Only process these search_engine_id values (repeatable; ignores status/needs filters)")
    args = parser.parse_args()

    db = read_db()
    houses: Dict[str, dict] = db.get("houses", {})

    targets: List[Tuple[str, dict]] = []
    for hid, h in houses.items():
        if h.get("search_engine") != "mercadolibre":
            continue
        if args.engine_id:
            if h.get("search_engine_id") not in args.engine_id:
                continue
        else:
            if not args.include_removed and h.get("status") == "removed":
                continue
            if not _needs_repair(h, args.max_images):
                continue
        if not h.get("url"):
            continue
        targets.append((hid, h))

    if not targets:
        print("No MercadoLibre houses need image repair.")
        return

    if args.limit > 0:
        targets = targets[: args.limit]

    print(f"Found {len(targets)} ML houses to repair "
          f"(stored images ≤ {args.max_images})")

    if args.dry_run:
        for hid, h in targets:
            print(f"  {hid[:8]}… imgs={len(h.get('images') or [])}  {h.get('url', '')[-70:]}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="es-AR",
        )
        await context.add_init_script(_INIT_SCRIPT)

        sem = asyncio.Semaphore(args.concurrency)

        async def process_one(hid: str, h: dict) -> Tuple[str, str, dict]:
            url = h.get("url", "")
            async with sem:
                if args.delay:
                    await asyncio.sleep(args.delay)
                page = await context.new_page()
                try:
                    detail = await _scrape_detail(page, url)
                except Exception as exc:
                    return hid, f"error: {type(exc).__name__}", {}
                finally:
                    await page.close()

            new_images = detail.get("images") or []
            old_images = h.get("images") or []
            if not new_images:
                return hid, "no-data", {}
            if len(new_images) <= len(old_images):
                # Never downgrade: only a fuller gallery is a repair.
                return hid, "confirmed", {}
            return hid, "ok", {"images": new_images}

        tasks = [process_one(hid, h) for hid, h in targets]

        ok = 0
        confirmed = 0
        no_data = 0
        errors = 0
        batch: List[Tuple[str, dict]] = []

        done = asyncio.as_completed(tasks)
        for i, coro in enumerate(done):
            hid, status, changes = await coro
            h = houses.get(hid, {})
            if status == "ok":
                batch.append((hid, changes))
                ok += 1
                print(f"  [{i + 1}/{len(targets)}] {hid[:8]}… "
                      f"images {len(h.get('images') or [])}→{len(changes['images'])}",
                      flush=True)
            elif status == "confirmed":
                confirmed += 1
            elif status == "no-data":
                no_data += 1
                print(f"  [{i + 1}/{len(targets)}] {hid[:8]}… no images on page — left untouched", flush=True)
            else:
                errors += 1
                print(f"  [{i + 1}/{len(targets)}] {hid[:8]}… {status} — left untouched", flush=True)

            if len(batch) >= args.batch:
                _flush(batch)
                batch.clear()

        if batch:
            _flush(batch)

        await context.close()
        await browser.close()

    print(f"\nDone: {ok} galleries restored, {confirmed} already as full as the page, "
          f"{no_data} no data, {errors} errors")


def _flush(updates: List[Tuple[str, dict]]) -> None:
    """Atomically write a batch of image updates to db.json."""
    def apply(db: dict) -> None:
        for hid, changes in updates:
            house = db.get("houses", {}).get(hid)
            if not house:
                continue
            for key, value in changes.items():
                house[key] = value
            house["last_updated"] = _now()

    atomic_update(apply)
    print(f"  ✅ Wrote batch of {len(updates)} updates")


if __name__ == "__main__":
    asyncio.run(main())
