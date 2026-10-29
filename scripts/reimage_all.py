#!/usr/bin/env python3
"""Re-collect the image gallery of every house from its detail page.

Why this exists
---------------
A house's gallery comes from its detail page, but a normal run never refreshes
one: known listings skip the detail page (``existing_ids``) and the search card
only carries a thumbnail (MercadoLibre: 1 image, Argenprop: <= 5).  Whatever is
stored is what the user sees, so a clobbering bug or a blocked run that lost the
detail-page photos stays lost until something re-visits the page.

This tool re-visits every house with the same per-engine detail extractor used
by the scraper and writes the result under the shrink-proof replacement rule:
a stored gallery is only replaced by one at least as long. An empty or shorter
result is never written, so re-running can never degrade a record.

Only ``images`` and ``last_updated`` change.  Price, surfaces, price history,
review, notes, geo-coordinates and the publish-tracking fields are never
touched, and a house whose gallery is already as full as the page is left
completely untouched.

Usage (from anywhere):
    python scripts/reimage_all.py --dry-run                 # preview targets
    python scripts/reimage_all.py                           # all active houses
    python scripts/reimage_all.py --engine mercadolibre     # one portal
    python scripts/reimage_all.py --engine remax            # API-backed portal
    python scripts/reimage_all.py --max-images 4            # only thin galleries
    python scripts/reimage_all.py --include-removed         # also status=removed
    python scripts/reimage_all.py --limit 20 --batch 10     # a quick sample
    python scripts/reimage_all.py --concurrency 3 --delay 0.5
    python scripts/reimage_all.py --log /tmp/opencode/reimage.log
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
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(_THIS, "backend"))

from playwright.async_api import async_playwright
from config import settings
from storage import _now, atomic_update, read_db

# Supported engines, in processing order (fastest / most affected first).
SUPPORTED_ENGINES = ("mercadolibre", "argenprop", "zonaprop", "remax")

_INIT_SCRIPT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


def _detail_fetchers() -> Dict[str, Callable]:
    """Per-engine detail extractor — the scraper's own, never a copy."""
    from scrapers.argenprop import _scrape_detail as argenprop_detail
    from scrapers.mercadolibre import _scrape_detail as mercadolibre_detail
    from scrapers.remax import _fetch_detail_api as remax_detail
    from scrapers.zonaprop import _scrape_detail as zonaprop_detail

    return {
        "mercadolibre": mercadolibre_detail,
        "zonaprop": zonaprop_detail,
        "argenprop": argenprop_detail,
        "remax": remax_detail,
    }


# ── Selection and write rules (pure, so they are unit-testable) ───────────


def select_targets(
    houses: Dict[str, dict],
    engines: Tuple[str, ...],
    *,
    include_removed: bool = False,
    max_images: int = 0,
    ids: Optional[set] = None,
) -> Dict[str, List[Tuple[str, dict]]]:
    """Group the houses to re-image by engine.

    ``max_images`` > 0 keeps only houses whose stored gallery is that short or
    shorter — the way to target the houses a clobbering bug stripped.
    """
    out: Dict[str, List[Tuple[str, dict]]] = {engine: [] for engine in engines}
    for hid, house in houses.items():
        engine = house.get("search_engine")
        if engine not in out:
            continue
        if not house.get("url"):
            continue
        if not include_removed and house.get("status") == "removed":
            continue
        if max_images and len(house.get("images") or []) > max_images:
            continue
        if ids and hid not in ids and house.get("search_engine_id") not in ids:
            continue
        out[engine].append((hid, house))
    return {engine: pairs for engine, pairs in out.items() if pairs}


def _remax_filters_by_session(db: dict) -> Dict[str, List[str]]:
    """Collect saved Remax search filters so legacy entity IDs can be recovered."""
    filters: Dict[str, List[str]] = {}
    for user in db.get("users", {}).values():
        for session_id, session in user.get("sessions", {}).items():
            session_filters = [
                source.get("filter")
                for source in session.get("search_sources", [])
                if source.get("engine") == "remax" and source.get("filter")
            ]
            if session_filters:
                filters.setdefault(session_id, []).extend(session_filters)
    return {
        session_id: list(dict.fromkeys(session_filters))
        for session_id, session_filters in filters.items()
    }


def replacement(
    stored: Optional[List[str]], fetched: Optional[List[str]]
) -> Optional[List[str]]:
    """The gallery to write, or None to leave the stored one alone.

    Mirrors ``house_fields.sync_mutable_fields``: never shorter, never empty,
    and an identical list is a no-op rather than a rewrite.
    """
    old = [u for u in (stored or []) if isinstance(u, str) and u]
    new = [u for u in (fetched or []) if isinstance(u, str) and u.startswith("http")]
    if not new:
        return None
    if old and len(new) < len(old):
        return None
    if new == old:
        return None
    return new


# ── Writing ──────────────────────────────────────────────────────────────


def _write_batch(updates: List[Tuple[str, List[str]]], log) -> None:
    """Atomically store new galleries; images and last_updated only."""
    if not updates:
        return

    def apply(db: dict) -> None:
        for hid, images in updates:
            house = db.get("houses", {}).get(hid)
            if not house:
                continue
            house["images"] = images
            house["last_updated"] = _now()

    atomic_update(apply)
    log(f"  💾 wrote {len(updates)} galleries")


# ── One engine ───────────────────────────────────────────────────────────


async def _images_from_detail(
    page,
    engine: str,
    fetcher,
    house: dict,
    resolved_entity_ids: Optional[Dict[str, str]] = None,
) -> Optional[List[str]]:
    if engine == "remax":
        from scrapers.remax import _resolve_remax_entity_id

        entity_id = house.get("remax_entity_id")
        search_engine_id = str(house.get("search_engine_id") or "")
        if not entity_id and resolved_entity_ids:
            entity_id = resolved_entity_ids.get(search_engine_id)
        if not entity_id:
            entity_id = await _resolve_remax_entity_id(
                page, house["url"], search_engine_id
            )
        if not entity_id:
            return None
        detail = await fetcher(entity_id)
        return detail.get("images") if isinstance(detail, dict) else None

    detail = await fetcher(page, house["url"])
    if not isinstance(detail, dict):
        return None
    return detail.get("images")


def _blocked_error_type():
    from scrapers.base import ScrapeBlockedError

    return ScrapeBlockedError


async def _open_engine(pw, engine: str):
    """Return (browser, context, headless, profile_dir).

    Zonaprop reuses the app's persistent profile — the same directory the
    daily run uses, so a challenge it already solved is not re-solved here.
    """
    if engine == "zonaprop":
        from scrapers.zonaprop import _HEADLESS, _PROFILE_DIR

        os.makedirs(_PROFILE_DIR, exist_ok=True)
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=_PROFILE_DIR,
            headless=_HEADLESS,
            user_agent=settings.user_agent,
            viewport={"width": 1280, "height": 900},
            locale="es-AR",
            args=[
                "--disable-blink-features=AutomationControlled",
                *(
                    ["--no-sandbox", "--disable-dev-shm-usage", "--log-level=3"]
                    if _HEADLESS
                    else ["--start-minimized"]
                ),
            ],
        )
        if _HEADLESS:
            probe = await context.new_page()
            blocked_types = {"image", "media", "font"}
            await probe.route(
                "**/*",
                lambda route, req: (
                    route.abort() if req.resource_type in blocked_types else route.continue_()
                ),
            )
            await probe.close()
        return None, context, _HEADLESS, _PROFILE_DIR

    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=settings.user_agent,
        viewport={"width": 1280, "height": 900},
        locale="es-AR",
    )
    await context.add_init_script(_INIT_SCRIPT)
    return browser, context, True, None


async def process_engine(
    pw,
    engine: str,
    pairs: List[Tuple[str, dict]],
    args,
    fetchers,
    log,
    remax_filters_by_session: Optional[Dict[str, List[str]]] = None,
) -> dict:
    stats = {"ok": 0, "confirmed": 0, "no_data": 0, "errors": 0, "blocked": 0, "aborted": 0}
    browser, context, headless, profile = await _open_engine(pw, engine)
    log(
        f"  {engine}: {len(pairs)} houses · {args.concurrency} tabs · "
        f"{'headless' if headless else 'headed'}"
        + (f" · profile {profile}" if profile else "")
    )

    fetcher = fetchers[engine]
    blocked_type = _blocked_error_type()
    sem = asyncio.Semaphore(args.concurrency)
    state = {"blocks_in_a_row": 0, "stop": False}
    batch: List[Tuple[str, List[str]]] = []
    resolved_entity_ids: Dict[str, str] = {}

    if engine == "remax":
        missing_ids = {
            str(house.get("search_engine_id") or "")
            for _, house in pairs
            if not house.get("remax_entity_id") and house.get("search_engine_id")
        }
        filters = list(dict.fromkeys(
            search_filter
            for _, house in pairs
            for search_filter in (remax_filters_by_session or {}).get(
                str(house.get("session_id") or ""), []
            )
        ))
        if not filters and remax_filters_by_session:
            filters = list(dict.fromkeys(
                search_filter
                for session_filters in remax_filters_by_session.values()
                for search_filter in session_filters
            ))
        if missing_ids and filters:
            lookup_page = await context.new_page()
            try:
                from scrapers.remax import _resolve_remax_entity_ids_from_searches

                resolved_entity_ids = await _resolve_remax_entity_ids_from_searches(
                    lookup_page, filters, missing_ids
                )
                log(
                    f"  remax: recovered {len(resolved_entity_ids)}/{len(missing_ids)} "
                    "legacy entity IDs from saved searches"
                )
            except blocked_type:
                log("  remax: saved-search lookup was blocked; trying per-listing recovery")
            except Exception as exc:
                log(
                    "  remax: saved-search lookup failed "
                    f"({type(exc).__name__}); trying per-listing recovery"
                )
            finally:
                await lookup_page.close()

    async def process_one(hid: str, house: dict) -> Tuple[str, str, Optional[List[str]], int, str]:
        async with sem:
            if state["stop"]:
                return hid, "skipped", None, 0, ""
            if args.delay:
                await asyncio.sleep(args.delay)
            page = await context.new_page()
            try:
                fetched = await _images_from_detail(
                    page, engine, fetcher, house, resolved_entity_ids
                )
                state["blocks_in_a_row"] = 0
            except Exception as exc:
                if isinstance(exc, blocked_type):
                    state["blocks_in_a_row"] += 1
                    reason = type(exc).__name__
                    if state["blocks_in_a_row"] >= args.max_blocks:
                        state["stop"] = True
                        return hid, "blocked-stop", None, 0, reason
                    return hid, "blocked", None, 0, reason
                return hid, f"error:{type(exc).__name__}", None, 0, ""
            finally:
                await page.close()

        new_images = replacement(house.get("images"), fetched)
        old_count = len(house.get("images") or [])
        if new_images is not None:
            return hid, "ok", new_images, old_count, house.get("search_engine_id") or ""
        if not fetched:
            return hid, "no-data", None, old_count, ""
        return hid, "confirmed", None, old_count, ""

    tasks = [asyncio.ensure_future(process_one(hid, house)) for hid, house in pairs]
    for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
        hid, status, images, old_count, label = await coro
        if status == "ok":
            stats["ok"] += 1
            batch.append((hid, images))
            log(f"  [{i}/{len(pairs)}] {engine} {label or hid[:8]} images {old_count}→{len(images)}")
            if len(batch) >= args.batch:
                _write_batch(batch, log)
                batch.clear()
        elif status == "confirmed":
            stats["confirmed"] += 1
        elif status == "no-data":
            stats["no_data"] += 1
            log(f"  [{i}/{len(pairs)}] {engine} {label or hid[:8]} no images on page — untouched")
        elif status in ("blocked", "blocked-stop"):
            stats["blocked"] += 1
            log(f"  [{i}/{len(pairs)}] {engine} {label or hid[:8]} blocked — counted")
        elif status == "skipped":
            pass
        else:
            stats["errors"] += 1
            log(f"  [{i}/{len(pairs)}] {engine} {label or hid[:8]} {status.split(':', 1)[-1]} — untouched")

    _write_batch(batch, log)

    await context.close()
    if browser is not None:
        await browser.close()

    if state["stop"]:
        stats["aborted"] = 1
        log(f"  ⚠️  {engine}: stopped early after {args.max_blocks} blocks in a row")

    log(
        f"  {engine}: done — {stats['ok']} galleries written, {stats['confirmed']} already complete, "
        f"{stats['no_data']} no data, {stats['blocked']} blocked, {stats['errors']} errors\n"
    )
    return stats


# ── Main ─────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-collect every house's image gallery from its detail page"
    )
    parser.add_argument(
        "--engine",
        action="append",
        choices=list(SUPPORTED_ENGINES) + ["all"],
        default=None,
        help="Portal to re-image (repeatable; default: all supported)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Only process N houses per engine")
    parser.add_argument("--dry-run", action="store_true", help="Show targets without writing")
    parser.add_argument(
        "--include-removed",
        action="store_true",
        help="Also process houses with status=removed (usually pages that no longer answer)",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=0,
        help="Only re-image houses with at most N stored images (0 = no filter)",
    )
    parser.add_argument(
        "--id",
        action="append",
        default=[],
        metavar="ID",
        help="Only these internal ids or search_engine_id values (repeatable)",
    )
    parser.add_argument("--batch", type=int, default=50, help="Write every N repairs (default: 50)")
    parser.add_argument(
        "--concurrency", type=int, default=3, help="Concurrent tabs per engine (default: 3)"
    )
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds before each fetch")
    parser.add_argument(
        "--max-blocks",
        type=int,
        default=5,
        help="Stop an engine after this many blocks in a row (default: 5)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Force headless (Docker) browsers; default mirrors the app per portal",
    )
    parser.add_argument("--log", metavar="FILE", help="Also append progress to FILE")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.headless:
        os.environ["PLAYWRIGHT_HEADLESS"] = "1"

    engines = SUPPORTED_ENGINES
    if args.engine:
        chosen = [e for e in args.engine if e != "all"]
        engines = tuple(e for e in SUPPORTED_ENGINES if e in chosen) or SUPPORTED_ENGINES

    log_file = None
    if args.log:
        log_file = open(args.log, "w")

    def log(message: str = "") -> None:
        print(message, flush=True)
        if log_file:
            log_file.write(message + "\n")
            log_file.flush()

    db = read_db()
    houses = db.get("houses", {})
    remax_filters_by_session = _remax_filters_by_session(db)
    targets = select_targets(
        houses,
        engines,
        include_removed=args.include_removed,
        max_images=args.max_images,
        ids=set(args.id) if args.id else None,
    )

    log(f"=== reimage_all · {_now()} ===")
    log(f"db: {len(houses)} houses · engines: {', '.join(engines)}")
    for engine in engines:
        log(f"  {engine}: {len(targets.get(engine, []))} target houses")

    total = sum(len(p) for p in targets.values())
    if args.limit:
        total = sum(min(args.limit, len(p)) for p in targets.values())
    log(f"total targets: {total}\n")

    if args.dry_run:
        for engine, pairs in targets.items():
            shown = pairs[: args.limit] if args.limit else pairs
            log(f"{engine}: {len(pairs)} houses (showing {len(shown)})")
            for hid, house in shown[:50]:
                log(
                    f"  {hid[:8]}… imgs={len(house.get('images') or [])}  "
                    f"{(house.get('search_engine_id') or '')} {house['url'][-70:]}"
                )
            if len(pairs) > 20:
                log(f"  … {len(pairs)} total")
    elif not targets:
        log("Nothing to do.")
    else:
        fetchers = _detail_fetchers()
        totals = {k: 0 for k in ("ok", "confirmed", "no_data", "errors", "blocked", "aborted")}
        async with async_playwright() as pw:
            for engine in SUPPORTED_ENGINES:
                pairs = targets.get(engine)
                if not pairs:
                    continue
                if args.limit:
                    pairs = pairs[: args.limit]
                stats = await process_engine(
                    pw, engine, pairs, args, fetchers, log, remax_filters_by_session
                )
                for key in stats:
                    totals[key] = totals.get(key, 0) + stats[key]

        log(
            f"=== DONE: {totals['ok']} galleries refreshed, "
            f"{totals['confirmed']} already complete, {totals['no_data']} no data, "
            f"{totals['blocked']} blocked, {totals['errors']} errors ==="
        )

    if log_file:
        log_file.close()


if __name__ == "__main__":
    asyncio.run(main())
