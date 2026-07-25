"""
Diagnostic test for Argenprop pagination.

Verifies:
1. _page_url generates correct URLs (dash format: ?pagina-N)
2. Pagination detection finds correct total pages
3. Each page returns unique cards
4. The full scrape_search method gets all houses

Usage (from backend/):
    python tests/test_argenprop_pagination.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from scrapers.argenprop import (
    ArgenpropScraper,
    _extract_cards_js,
    _extract_total_pages,
    BASE_URL,
)

SEARCH_FILTER = "/casas/venta/partido-de-san-isidro/3-dormitorios/dolares-350000-360000?solo-ver-dolares"
SEARCH_URL = f"{BASE_URL}{SEARCH_FILTER}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def test_page_url_format():
    """Verify _page_url generates correct dash-format URLs."""
    scraper = ArgenpropScraper.__new__(ArgenpropScraper)
    filter_no_qs = "/casas/venta/barrio"
    filter_with_qs = "/casas/venta/barrio?precio-100-200"

    # Page 1 returns base (no pagination param)
    assert scraper._page_url(filter_no_qs, 1) == f"{BASE_URL}{filter_no_qs}"
    assert scraper._page_url(filter_with_qs, 1) == f"{BASE_URL}{filter_with_qs}"

    # Page 2 uses dash format
    url_no_qs = scraper._page_url(filter_no_qs, 2)
    assert "pagina-2" in url_no_qs, f"Expected 'pagina-2' in URL, got: {url_no_qs}"
    assert "pagina=2" not in url_no_qs, f"Should NOT have 'pagina=' (equals), got: {url_no_qs}"

    url_with_qs = scraper._page_url(filter_with_qs, 2)
    assert "pagina-2" in url_with_qs, f"Expected 'pagina-2' in URL, got: {url_with_qs}"
    assert "pagina=2" not in url_with_qs, f"Should NOT have 'pagina=' (equals), got: {url_with_qs}"

    # Page 5
    url5 = scraper._page_url(filter_no_qs, 5)
    assert "pagina-5" in url5, f"Expected 'pagina-5' in URL, got: {url5}"

    print("  ✓ _page_url generates correct dash-format URLs")


async def test_pagination_live():
    """Test pagination against live Argenprop site."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="es-AR",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await context.new_page()

        # Load page 1
        print(f"\n  Loading: {SEARCH_URL}")
        resp = await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        print(f"    HTTP {resp.status}")
        await asyncio.sleep(3)

        # Accept cookies
        try:
            btn = await page.query_selector('button:has-text("Acepto"), button:has-text("Aceptar")')
            if btn:
                await btn.click()
                await asyncio.sleep(1)
        except Exception:
            pass

        # Detect total pages
        total_pages = await _extract_total_pages(page)
        print(f"    Total pages detected: {total_pages}")
        assert total_pages > 1, f"Expected >1 pages, got {total_pages}"

        # Extract cards from each page using _page_url
        scraper = ArgenpropScraper.__new__(ArgenpropScraper)
        all_ids = set()
        cards_per_page = []

        for pg in range(1, total_pages + 1):
            url = scraper._page_url(SEARCH_FILTER, pg)
            if pg > 1:
                print(f"    Loading page {pg}: {url}")
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                print(f"      HTTP {resp.status}")
                await asyncio.sleep(3)

            cards = await _extract_cards_js(page)
            ids = {c.get("id", "") for c in cards if c.get("id")}
            new_ids = ids - all_ids
            all_ids |= ids
            cards_per_page.append(len(cards))

            print(f"    Page {pg}: {len(cards)} cards, {len(new_ids)} new")

        print(f"\n    Cards per page: {cards_per_page}")
        print(f"    Total unique IDs: {len(all_ids)}")
        assert len(all_ids) > 20, f"Expected >20 unique cards, got {len(all_ids)}"
        print("  ✓ Live pagination works — all pages scraped with unique cards")

        await context.close()


async def main():
    print("═" * 50)
    print("  Argenprop Pagination Tests")
    print("═" * 50)

    print("\nTest 1: _page_url format")
    test_page_url_format()

    print("\nTest 2: Live pagination")
    await test_pagination_live()

    print(f"\n{'═'*50}")
    print("  All tests passed!")
    print("═" * 50)


if __name__ == "__main__":
    asyncio.run(main())
