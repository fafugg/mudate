#!/usr/bin/env python3
"""Re-fetch MercadoLibre detail pages to repair wrong "metros" fields.

Background: the ML search-card parser used to store the polycard floorSize
(the covered surface) as BOTH covered_m2 and total_m2, and persistence
re-merged those card values over the stored record on every daily run — so
even correctly extracted totals were clobbered back to the covered value.
387 of 470 ML houses ended up with total == covered.

The scraper is fixed; this script repairs the historical records by visiting
each affected detail page and re-reading "Superficie total" / "Superficie
cubierta" from the specs table.

Only covered_m2 / total_m2 / price_per_m2 are touched; review, notes,
geo-coordinates and price history are never modified.  Houses whose detail
page yields no surface data at all are left untouched.

Usage (from anywhere):
    python scripts/repair_ml_m2.py --dry-run                 # preview targets
    python scripts/repair_ml_m2.py                           # repair suspicious
    python scripts/repair_ml_m2.py --all                     # re-audit every house
    python scripts/repair_ml_m2.py --limit 5                 # first 5 only
    python scripts/repair_ml_m2.py --batch 50                # batch writes
    python scripts/repair_ml_m2.py --concurrency 3           # parallel tabs
    python scripts/repair_ml_m2.py --delay 0.5               # pace the requests
    python scripts/repair_ml_m2.py --include-removed         # also removed

Only values actually observed on a detail page are written: a dimension the
page does not report (or a page that cannot be opened) leaves the stored value
untouched, so a run can never degrade a record.
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
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(_THIS, "backend"))

from playwright.async_api import async_playwright
from config import settings
from scrapers.mercadolibre import _scrape_detail
from storage import _now, atomic_update, read_db

UA = settings.user_agent
_INIT_SCRIPT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


def _needs_repair(h: dict) -> bool:
    """True when the stored m2 pair is suspicious or incomplete."""
    total, cov = h.get("total_m2"), h.get("covered_m2")
    if total is not None and cov is not None:
        return total <= cov  # equal (suspected clobber) or invalid (<)
    # one of the two missing → worth filling in from the detail page
    return total is not None or cov is not None


def _fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:g}"


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repair MercadoLibre covered/total m2 from detail pages"
    )
    parser.add_argument("--limit", type=int, default=0, help="Only process N houses")
    parser.add_argument("--dry-run", action="store_true", help="Show targets without writing")
    parser.add_argument("--batch", type=int, default=50, help="Write updates in batches of N (default: 50)")
    parser.add_argument("--include-removed", action="store_true", help="Include houses with status=removed")
    parser.add_argument("--concurrency", type=int, default=3, help="Number of concurrent tabs (default: 3)")
    parser.add_argument("--all", action="store_true",
                        help="Re-audit every ML house with a URL, not only the suspicious ones")
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
            if not args.all and not _needs_repair(h):
                continue
        if not h.get("url"):
            continue
        targets.append((hid, h))

    if not targets:
        print("No MercadoLibre houses need m2 repair.")
        return

    if args.limit > 0:
        targets = targets[: args.limit]

    n_eq = sum(1 for _, h in targets
               if h.get("total_m2") is not None and h.get("total_m2") == h.get("covered_m2"))
    print(f"Found {len(targets)} ML houses to repair "
          f"({n_eq} with total == covered, {len(targets) - n_eq} incomplete/invalid)")

    if args.dry_run:
        for hid, h in targets:
            print(f"  {hid[:8]}… total={_fmt(h.get('total_m2')):>8} "
                  f"covered={_fmt(h.get('covered_m2')):>8}  {h.get('url', '')[-70:]}")
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

            d_total, d_cov = detail.get("total_m2"), detail.get("covered_m2")
            if d_total is None and d_cov is None:
                return hid, "no-data", {}

            # Observed values win; a dimension the page does not report keeps
            # whatever is stored (a run must never erase a good value).
            new_cov = d_cov if d_cov is not None else h.get("covered_m2")
            new_tot = d_total if d_total is not None else h.get("total_m2")
            if new_tot is not None and new_cov is not None and new_tot < new_cov:
                new_tot = None  # impossible pair → keep covered only

            changes: dict = {}
            if new_cov != h.get("covered_m2"):
                changes["covered_m2"] = new_cov
            if new_tot != h.get("total_m2"):
                changes["total_m2"] = new_tot
            if changes:
                price = h.get("price")
                m2 = new_cov or new_tot
                changes["price_per_m2"] = round(price / m2, 2) if price and m2 else None
                return hid, "ok", changes
            return hid, "confirmed", {}

        tasks = [process_one(hid, h) for hid, h in targets]

        ok = 0
        confirmed = 0
        no_data = 0
        errors = 0
        still_equal = 0
        batch: List[Tuple[str, dict]] = []

        done = asyncio.as_completed(tasks)
        for i, coro in enumerate(done):
            hid, status, changes = await coro
            h = houses.get(hid, {})
            if status == "ok":
                batch.append((hid, changes))
                ok += 1
                if changes.get("total_m2") == changes.get("covered_m2") or (
                    changes.get("total_m2") is None
                ):
                    still_equal += 1
                print(f"  [{i + 1}/{len(targets)}] {hid[:8]}… "
                      f"total {_fmt(h.get('total_m2'))}→{_fmt(changes.get('total_m2', h.get('total_m2')))}  "
                      f"covered {_fmt(h.get('covered_m2'))}→{_fmt(changes.get('covered_m2', h.get('covered_m2')))}",
                      flush=True)
            elif status == "confirmed":
                confirmed += 1
            elif status == "no-data":
                no_data += 1
                print(f"  [{i + 1}/{len(targets)}] {hid[:8]}… no surface data on page — left untouched", flush=True)
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

    print(f"\nDone: {ok} updated ({still_equal} still equal/absent after repair — likely legit), "
          f"{confirmed} already correct, {no_data} no data, {errors} errors")


def _flush(updates: List[Tuple[str, dict]]) -> None:
    """Atomically write a batch of m2 updates to db.json."""
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
