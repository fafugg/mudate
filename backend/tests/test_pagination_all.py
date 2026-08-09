"""
Pagination verification tests for all scrapers.

Tests that each scraper:
1. Extracts correct paging info (total, totalPages)
2. Loads all pages without missing any
3. Produces no duplicate search_engine_id across pages
4. Has no empty pages
5. All results have valid URLs

Usage (from backend/):
    .venv/bin/python tests/test_pagination_all.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright

# ── Test URLs (small result sets for fast testing) ──────────────────────────

ZONAPROP_FILTER = "/casas-ph-venta-san-isidro-martinez-acassuso-beccar-340000-350000-dolar.html"
ZONAPROP_BASE = "https://www.zonaprop.com.ar"

ARGENPROP_FILTER = "/casas/venta/partido-de-san-isidro/3-dormitorios/dolares-350000-360000?solo-ver-dolares"
ARGENPROP_BASE = "https://www.argenprop.com"

ML_FILTER = "/3-dormitorios/bsas-gba-norte/san-isidro/_PriceRange_330000USD-340000USD_NoIndex_True?loader=true"
ML_BASE = "https://inmuebles.mercadolibre.com.ar"

REMAX_FILTER = "/?page=0&pageSize=24&sort=-createdAt&in:operationId=1&in:typeId=9,10,11&pricein=1:350000:360000&locations=in:::116@%3Cb%3ESan%3C%2Fb%3E%20%3Cb%3EIsidro%3C%2Fb%3E::::&landingPath=&filterCount=2&viewMode=listViewMode"
REMAX_BASE = "https://www.remax.com.ar"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Minimum expected results per engine (catches total failures)
MIN_EXPECTED = {
    "zonaprop": 50,
    "argenprop": 10,
    "mercadolibre": 10,
    "remax": 3,
}


class TestResult:
    def __init__(self, engine: str):
        self.engine = engine
        self.checks = []
        self.passed = True
        self.skipped = False
        self.skip_reason = ""

    def check(self, name: str, ok: bool, detail: str = ""):
        status = "✓" if ok else "✗"
        msg = f"  {status} {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        self.checks.append((name, ok))
        if not ok:
            self.passed = False

    def skip(self, reason: str):
        print(f"  ⊘ {reason}")
        self.skipped = True
        self.skip_reason = reason

    def summary(self):
        if self.skipped:
            print(f"  ⊘ SKIP — {self.engine}: {self.skip_reason}\n")
            return True  # treated as non-failure
        icon = "✓ PASS" if self.passed else "✗ FAIL"
        n_ok = sum(1 for _, ok in self.checks if ok)
        print(f"  {icon} ({n_ok}/{len(self.checks)} checks)\n")
        return self.passed


# ── Zonaprop ────────────────────────────────────────────────────────────────

async def test_zonaprop(page) -> TestResult:
    from scrapers.zonaprop import _collect_all_pages, _extract_paging_info

    r = TestResult("Zonaprop")
    print("Zonaprop:")

    t0 = time.time()
    all_raw, paging_info = await _collect_all_pages(
        page, ZONAPROP_FILTER, None, None
    )
    elapsed = time.time() - t0

    total = paging_info.get("total", 0)
    total_pages = paging_info.get("totalPages", 0)

    r.check("paging_info extracted", bool(paging_info and total > 0),
            f"total={total}, totalPages={total_pages}")
    r.check("minimum results", len(all_raw) >= MIN_EXPECTED["zonaprop"],
            f"got {len(all_raw)}, need >={MIN_EXPECTED['zonaprop']}")
    r.check("page count reasonable", total_pages > 0,
            f"totalPages={total_pages}")

    # Check for duplicate IDs
    ids = [c.get("id", "") for c in all_raw if c.get("id")]
    unique_ids = set(ids)
    r.check("no duplicate IDs", len(ids) == len(unique_ids),
            f"{len(ids)} total, {len(unique_ids)} unique")

    # Check all results have URLs
    no_url = sum(1 for c in all_raw if not c.get("urlPath") and not c.get("id"))
    r.check("all results have URL", no_url == 0,
            f"{no_url} missing URL" if no_url else "")

    print(f"  ({elapsed:.1f}s, {len(all_raw)} cards from ~{total_pages} pages)")
    return r


# ── Argenprop ───────────────────────────────────────────────────────────────

async def test_argenprop(page) -> TestResult:
    from scrapers.argenprop import (
        ArgenpropScraper, _extract_cards_js, _extract_total_pages, _extract_total_items,
        BASE_URL,
    )

    r = TestResult("Argenprop")
    print("Argenprop:")

    t0 = time.time()

    # Load first page
    url = f"{BASE_URL}{ARGENPROP_FILTER}"
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)

    if resp and resp.status == 403:
        r.check("page loaded", False, "HTTP 403")
        return r

    # Accept cookies
    try:
        btn = await page.query_selector('button:has-text("Acepto"), button:has-text("Aceptar")')
        if btn:
            await btn.click()
            await asyncio.sleep(1)
    except Exception:
        pass

    total_pages = await _extract_total_pages(page)
    total_items = await _extract_total_items(page)

    r.check("paging_info extracted", total_pages > 0,
            f"totalPages={total_pages}, total_items={total_items}")

    # Iterate all pages
    scraper = ArgenpropScraper.__new__(ArgenpropScraper)
    all_cards = []
    seen_ids = set()
    pages_loaded = 0
    empty_pages = 0

    for pg in range(1, total_pages + 1):
        pg_url = scraper._page_url(ARGENPROP_FILTER, pg)
        if pg > 1:
            await page.goto(pg_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

        cards = await _extract_cards_js(page)
        pages_loaded += 1

        if not cards:
            empty_pages += 1
            continue

        for c in cards:
            cid = c.get("id", "")
            if cid:
                seen_ids.add(cid)
            all_cards.append(c)

    elapsed = time.time() - t0

    r.check("all pages loaded", pages_loaded == total_pages,
            f"{pages_loaded}/{total_pages}")
    r.check("no empty pages", empty_pages == 0,
            f"{empty_pages} empty pages" if empty_pages else "")
    r.check("minimum results", len(all_cards) >= MIN_EXPECTED["argenprop"],
            f"got {len(all_cards)}, need >={MIN_EXPECTED['argenprop']}")
    r.check("no duplicate IDs", len(all_cards) == len(seen_ids),
            f"{len(all_cards)} total, {len(seen_ids)} unique")

    # Check total items estimate
    if total_items > 0:
        r.check("total items reasonable", len(all_cards) >= total_items * 0.7,
                f"got {len(all_cards)}, expected ~{total_items}")

    print(f"  ({elapsed:.1f}s, {len(all_cards)} cards from {pages_loaded} pages)")
    return r


# ── MercadoLibre ────────────────────────────────────────────────────────────

async def test_mercadolibre(page) -> TestResult:
    from scrapers.mercadolibre import (
        MercadoLibreScraper, _extract_paging_info, ML_PAGE_SIZE,
        BASE_URL,
    )

    r = TestResult("MercadoLibre")
    print("MercadoLibre:")

    t0 = time.time()

    # Load first page
    url = f"{BASE_URL}{ML_FILTER}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # Check for bot detection redirect
    if "account-verification" in page.url or "captcha" in page.url:
        r.skip("ML bot detection active — cannot test pagination in this session")
        print(f"  ({time.time() - t0:.1f}s, SKIPPED)")
        return r

    paging_info = await _extract_paging_info(page)
    total = paging_info.get("total", 0)
    total_pages = paging_info.get("totalPages", 0)

    r.check("paging_info extracted", bool(paging_info and total > 0),
            f"total={total}, totalPages={total_pages}")

    # Extract from first page
    scraper = MercadoLibreScraper.__new__(MercadoLibreScraper)
    html = await page.content()
    first_cards = scraper._extract_from_json_ld(html)
    all_ids = set()
    all_listings = []

    for card in first_cards:
        se_id = card.get("search_engine_id") or ""
        if se_id:
            all_ids.add(se_id)
        all_listings.append(card)

    pages_loaded = 1
    empty_pages = 0

    # Click through remaining pages
    for pg in range(2, total_pages + 1):
        # Try click first
        next_clicked = False
        try:
            current_page_num = await page.evaluate("""() => {
                const el = document.querySelector('.andes-pagination__button--current');
                return el ? parseInt(el.textContent.trim()) : 0;
            }""")
            next_btn = await page.query_selector(
                f'a[aria-label="Ir a la página {current_page_num + 1}"]'
            )
            if not next_btn:
                next_btn = await page.query_selector(
                    'li.andes-pagination__button:not(.andes-pagination__button--disabled):not(.andes-pagination__button--current) a'
                )
            if next_btn:
                try:
                    async with page.expect_navigation(timeout=10000):
                        await page.evaluate("(el) => { el.click(); }", next_btn)
                except Exception:
                    pass
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await asyncio.sleep(2)
                next_clicked = True
        except Exception:
            pass

        # Fallback: URL navigation
        if not next_clicked:
            offset = (pg - 1) * ML_PAGE_SIZE + 1
            next_url = scraper._page_url(ML_FILTER, offset)
            try:
                nav_resp = await page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
                if nav_resp and nav_resp.status == 200:
                    next_clicked = True
            except Exception:
                pass

        if not next_clicked:
            break

        # Check for bot detection on subsequent pages
        if "account-verification" in page.url:
            break

        html = await page.content()
        cards = scraper._extract_from_json_ld(html)
        pages_loaded += 1

        if not cards:
            empty_pages += 1
            continue

        page_has_new = False
        for card in cards:
            se_id = card.get("search_engine_id") or ""
            if se_id and se_id not in all_ids:
                all_ids.add(se_id)
                page_has_new = True
            all_listings.append(card)

        if not page_has_new:
            empty_pages += 1

    elapsed = time.time() - t0

    r.check("all pages loaded", pages_loaded == total_pages,
            f"{pages_loaded}/{total_pages}")
    r.check("no empty pages", empty_pages == 0,
            f"{empty_pages} empty/no-new pages" if empty_pages else "")
    r.check("minimum results", len(all_ids) >= MIN_EXPECTED["mercadolibre"],
            f"got {len(all_ids)}, need >={MIN_EXPECTED['mercadolibre']}")
    r.check("no duplicate IDs", len(all_listings) == len(all_ids),
            f"{len(all_listings)} total, {len(all_ids)} unique")

    if total > 0:
        r.check("total items reasonable", len(all_ids) >= total * 0.7,
                f"got {len(all_ids)}, expected ~{total}")

    print(f"  ({elapsed:.1f}s, {len(all_ids)} unique listings from {pages_loaded} pages)")
    return r


# ── Remax ───────────────────────────────────────────────────────────────────

async def test_remax(page) -> TestResult:
    from scrapers.remax import RemaxScraper, PAGE_SIZE, BASE_URL

    r = TestResult("Remax")
    print("Remax:")

    t0 = time.time()

    scraper = RemaxScraper.__new__(RemaxScraper)

    # Load first page
    url = scraper._page_url(REMAX_FILTER, 0)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # Accept cookies
    try:
        btn = await page.query_selector(
            'button:has-text("Acepto"), button:has-text("Aceptar"), button:has-text("OK")'
        )
        if btn:
            await btn.click()
            await asyncio.sleep(1)
    except Exception:
        pass

    # Extract ng-state
    ng_state = await page.evaluate("""() => {
        const el = document.getElementById('ng-state');
        if (!el) return null;
        try { return JSON.parse(el.textContent); } catch(e) { return null; }
    }""")

    card_listings, total_pages, total_items = scraper._parse_ng_state(ng_state)

    r.check("paging_info extracted", total_items > 0,
            f"total={total_items}, totalPages={total_pages}")

    all_ids = set()
    all_listings = list(card_listings)
    for card in card_listings:
        se_id = card.get("search_engine_id") or ""
        if se_id:
            all_ids.add(se_id)

    pages_loaded = 1
    empty_pages = 0

    # Iterate remaining pages
    for pg in range(1, total_pages):
        pg_url = scraper._page_url(REMAX_FILTER, pg)
        await page.goto(pg_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        ng_state = await page.evaluate("""() => {
            const el = document.getElementById('ng-state');
            if (!el) return null;
            try { return JSON.parse(el.textContent); } catch(e) { return null; }
        }""")

        cards, _, _ = scraper._parse_ng_state(ng_state)
        pages_loaded += 1

        if not cards:
            empty_pages += 1
            continue

        for card in cards:
            se_id = card.get("search_engine_id") or ""
            if se_id:
                all_ids.add(se_id)
            all_listings.append(card)

    elapsed = time.time() - t0

    r.check("all pages loaded", pages_loaded == total_pages,
            f"{pages_loaded}/{total_pages}")
    r.check("no empty pages", empty_pages == 0,
            f"{empty_pages} empty pages" if empty_pages else "")
    r.check("minimum results", len(all_ids) >= MIN_EXPECTED["remax"],
            f"got {len(all_ids)}, need >={MIN_EXPECTED['remax']}")
    r.check("no duplicate IDs", len(all_listings) == len(all_ids),
            f"{len(all_listings)} total, {len(all_ids)} unique")

    if total_items > 0:
        r.check("total items reasonable", len(all_ids) >= total_items * 0.7,
                f"got {len(all_ids)}, expected ~{total_items}")

    print(f"  ({elapsed:.1f}s, {len(all_ids)} unique listings from {pages_loaded} pages)")
    return r


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    print("═" * 55)
    print("  Pagination Verification Tests")
    print("═" * 55)
    print()

    results = []

    async with async_playwright() as p:
        # Zonaprop needs a persistent profile with Cloudflare clearance cookies
        # (the real scraper uses the same approach in production)
        zp_profile = os.path.expanduser("~/.mudate_browser")
        os.makedirs(zp_profile, exist_ok=True)
        zp_context = await p.chromium.launch_persistent_context(
            user_data_dir=zp_profile,
            headless=True,
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="es-AR",
            args=["--disable-blink-features=AutomationControlled"],
        )
        # Other scrapers can use a fresh context
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="es-AR",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )

        for test_fn in [test_zonaprop, test_argenprop, test_mercadolibre, test_remax]:
            # Use persistent context for Zonaprop
            ctx = zp_context if test_fn is test_zonaprop else context
            page = await ctx.new_page()
            try:
                result = await test_fn(page)
                results.append(result)
                result.summary()
            except Exception as e:
                r = TestResult(test_fn.__name__.replace("test_", ""))
                r.check("no exceptions", False, str(e)[:100])
                results.append(r)
                r.summary()
            finally:
                await page.close()

        await zp_context.close()
        await context.close()
        await browser.close()

    # Final summary
    print("═" * 55)
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    if passed == total:
        print(f"  All {total} scrapers passed!")
    else:
        failed = [r.engine for r in results if not r.passed]
        print(f"  {passed}/{total} passed. Failed: {', '.join(failed)}")
    print("═" * 55)

    return 0 if passed == total else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
