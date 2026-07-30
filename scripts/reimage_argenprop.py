#!/usr/bin/env python3
"""Re-scrape Argenprop house images using the fixed extraction logic.

Only updates houses where new images are successfully extracted (Option A).
Skips houses that error out, return no images, or lack a URL.

Usage:
    python reimage_argenprop.py                                # process all
    python reimage_argenprop.py --limit 5                      # first 5 only
    python reimage_argenprop.py --dry-run                      # preview only
    python reimage_argenprop.py --batch 100                    # batch writes
    python reimage_argenprop.py --include-removed              # also removed
    python reimage_argenprop.py --concurrency 5                # more concurrency
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
from scrapers.argenprop import _scrape_detail
from storage import _now, atomic_update, read_db

UA = settings.user_agent
_INIT_SCRIPT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


async def extract_images(page, url: str) -> List[str]:
    """Return images list from an Argenprop detail page, or [] on failure."""
    result = await _scrape_detail(page, url)
    return result.get("images", [])


async def main() -> None:
    parser = argparse.ArgumentParser(description="Re-scrape Argenprop house images")
    parser.add_argument("--limit", type=int, default=0, help="Only process N houses")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without writing")
    parser.add_argument("--batch", type=int, default=50, help="Write updates in batches of N (default: 50)")
    parser.add_argument("--include-removed", action="store_true", help="Include houses with status=removed")
    parser.add_argument("--concurrency", type=int, default=3, help="Number of concurrent scrapers (default: 3)")
    args = parser.parse_args()

    db = read_db()
    houses: Dict[str, dict] = db.get("houses", {})

    targets: List[Tuple[str, dict]] = []
    for hid, h in houses.items():
        if h.get("search_engine") != "argenprop":
            continue
        if not args.include_removed and h.get("status") == "removed":
            continue
        if not h.get("url"):
            continue
        targets.append((hid, h))

    if not targets:
        print("No Argenprop houses found to process.")
        return

    if args.limit > 0:
        targets = targets[:args.limit]

    print(f"Found {len(targets)} Argenprop houses to process")

    if args.dry_run:
        for hid, h in targets:
            print(f"  {hid}: {h.get('url', 'no-url')}")
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

        async def process_one(hid: str, h: dict) -> Tuple[str, str, List[str]]:
            url = h.get("url", "")
            async with sem:
                page = await context.new_page()
                try:
                    images = await extract_images(page, url)
                    await page.close()
                    if images:
                        return (hid, "ok", images)
                    return (hid, "no-images", [])
                except Exception as exc:
                    await page.close()
                    return (hid, f"error: {type(exc).__name__}", [])

        tasks = [process_one(hid, h) for hid, h in targets]

        ok = 0
        skipped = 0
        errors = 0
        batch: List[Tuple[str, List[str]]] = []

        done = asyncio.as_completed(tasks)
        for i, coro in enumerate(done):
            hid, status, images = await coro
            if status == "ok":
                batch.append((hid, images))
                ok += 1
            elif status == "no-images":
                skipped += 1
            else:
                errors += 1

            print(f"  [{i + 1}/{len(targets)}] {hid[:8]}... → {status}", flush=True)

            if len(batch) >= args.batch:
                _flush(batch)
                batch.clear()

        if batch:
            _flush(batch)

        await context.close()
        await browser.close()

    print(f"\nDone: {ok} updated, {skipped} skipped, {errors} errors")


def _flush(updates: List[Tuple[str, List[str]]]) -> None:
    """Atomically write a batch of image updates to db.json."""
    def apply(db: dict) -> None:
        for hid, images in updates:
            if hid in db.get("houses", {}):
                db["houses"][hid]["images"] = images
                db["houses"][hid]["last_updated"] = _now()

    atomic_update(apply)
    print(f"  \u270d  Wrote batch of {len(updates)} updates")


if __name__ == "__main__":
    asyncio.run(main())
