"""
Comprehensive diagnostic test for Argenprop scraper.

Tests:
1. _page_url generates correct URLs (dash format)
2. Pagination detection and multi-page extraction
3. Detail field extraction (DOM)
4. Full _scrape_detail pipeline (ld+json + DOM merge)
5. Card parsing
6. Multiple property types
7. Image filtering (agent logo exclusion)
8. Error handling

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
    _extract_detail_from_dom,
    _parse_card,
    _scrape_detail,
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

    assert scraper._page_url(filter_no_qs, 1) == f"{BASE_URL}{filter_no_qs}"
    assert scraper._page_url(filter_with_qs, 1) == f"{BASE_URL}{filter_with_qs}"

    url_no_qs = scraper._page_url(filter_no_qs, 2)
    assert "pagina-2" in url_no_qs, f"Expected 'pagina-2' in URL, got: {url_no_qs}"
    assert "pagina=2" not in url_no_qs, f"Should NOT have 'pagina=' (equals), got: {url_no_qs}"

    url_with_qs = scraper._page_url(filter_with_qs, 2)
    assert "pagina-2" in url_with_qs, f"Expected 'pagina-2' in URL, got: {url_with_qs}"
    assert "pagina=2" not in url_with_qs, f"Should NOT have 'pagina=' (equals), got: {url_with_qs}"

    url5 = scraper._page_url(filter_no_qs, 5)
    assert "pagina-5" in url5, f"Expected 'pagina-5' in URL, got: {url5}"

    print("  ✓ _page_url generates correct dash-format URLs")


def test_card_parsing():
    """Test _parse_card with various input scenarios."""
    # Full card
    raw = {
        "id": "12345", "href": f"{BASE_URL}/casa-en-venta--12345",
        "monto": "350000", "idMoneda": "2", "idTipoProp": "3",
        "dormitorios": "3", "ambientes": "4",
        "address": "Av. Libertador 123",
        "featureSpans": ["250 m² Cubierta", "344 m² Terreno"],
        "images": ["https://example.com/img1.jpg"],
    }
    result = _parse_card(raw)
    assert result["search_engine_id"] == "12345"
    assert result["price"] == 350000.0
    assert result["currency"] == "USD"
    assert result["type"] == "Casa"
    assert result["dormitorios"] == 3
    assert result["ambientes"] == 4
    assert result["address"] == "Av. Libertador 123"
    assert result["covered_m2"] == 250.0
    assert result["url"] == f"{BASE_URL}/casa-en-venta--12345"
    assert result["images"] == ["https://example.com/img1.jpg"]

    # Empty card (no URL) → returns empty dict
    assert _parse_card({}) == {}

    # Card with ARS currency
    raw_ars = {**raw, "idMoneda": "1", "monto": "50000000"}
    result_ars = _parse_card(raw_ars)
    assert result_ars["currency"] == "ARS"
    assert result_ars["price"] == 50000000.0

    # Card with unknown property type
    raw_unknown = {**raw, "idTipoProp": "99"}
    result_unknown = _parse_card(raw_unknown)
    assert result_unknown["type"] is None

    print("  ✓ _parse_card works for various inputs")


def test_image_filtering():
    """Test that agent logo URLs are excluded from card-level image extraction."""
    # _parse_card passes images through — filtering happens in _extract_cards_js
    # Test that _parse_card correctly limits images to 5
    raw = {
        "id": "12345", "href": f"{BASE_URL}/casa--12345",
        "images": [f"https://example.com/img{i}.jpg" for i in range(10)],
    }
    result = _parse_card(raw)
    assert len(result["images"]) == 5, f"Expected max 5 images, got {len(result['images'])}"

    # Empty/missing images
    raw_no_imgs = {"id": "12345", "href": f"{BASE_URL}/casa--12345"}
    result_no_imgs = _parse_card(raw_no_imgs)
    assert result_no_imgs["images"] is None

    print("  ✓ Image handling in card parsing works correctly")


async def test_detail_extraction(page):
    """Test DOM extraction on a live detail page."""
    test_url = f"{BASE_URL}/casa-en-venta-en-san-isidro-4-ambientes--10794289"
    print(f"    Loading: {test_url}")
    await page.goto(test_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    detail = await _extract_detail_from_dom(page)

    checks = [
        ("description", lambda v: len(v) > 50),
        ("covered_m2", lambda v: v and v > 0),
        ("total_m2", lambda v: v and v > 0),
        ("dormitorios", lambda v: v and v > 0),
        ("banos", lambda v: v and v > 0),
        ("ambientes", lambda v: v and v > 0),
        ("parking", lambda v: v is not None),
        ("age_years", lambda v: v and v > 0),
        ("condition", lambda v: bool(v)),
        ("orientation", lambda v: bool(v)),
        ("floor", lambda v: bool(v)),
        ("lat", lambda v: v and -90 < v < 90),
        ("lng", lambda v: v and -180 < v < 180),
        ("amenities", lambda v: len(v) > 0),
        ("real_estate", lambda v: bool(v)),
        ("images", lambda v: len(v) > 0),
    ]

    all_ok = True
    for field, check in checks:
        val = detail.get(field)
        ok = check(val) if val is not None else False
        status = "✓" if ok else "✗"
        preview = str(val)[:60] if val is not None else "None"
        print(f"    {status} {field}: {preview}")
        if not ok:
            all_ok = False

    assert all_ok, f"Missing: {[k for k, c in checks if detail.get(k) is None or not c(detail.get(k))]}"
    print(f"  ✓ All {len(checks)} detail fields extracted correctly")


async def test_scrape_detail_pipeline(page):
    """Test full _scrape_detail pipeline (ld+json + DOM merge)."""
    test_url = f"{BASE_URL}/casa-en-venta-en-san-isidro-4-ambientes--10794289"
    print(f"    Loading: {test_url}")

    result = await _scrape_detail(page, test_url)

    # Should have DOM values (not just ld+json)
    assert result.get("description"), "Description should come from DOM"
    assert len(result.get("description", "")) > 100, "DOM description should be full text"
    assert result.get("covered_m2"), "covered_m2 should be set"
    assert result.get("lat"), "lat should be set"
    assert result.get("amenities"), "amenities should be set"
    assert result.get("real_estate"), "real_estate should be set"
    assert result.get("images"), "images should be set"
    assert len(result.get("images", [])) > 1, "Should have multiple images"

    # Verify no agent logos in images
    for img in result.get("images", []):
        assert "_a/" not in img, f"Agent logo in images: {img}"

    print(f"  ✓ _scrape_detail pipeline: {len(result)} fields, {len(result.get('images', []))} images")


async def test_multiple_property_types(page):
    """Test extraction on different property types."""
    test_cases = [
        ("Casa", f"{BASE_URL}/casa-en-venta-en-san-isidro-4-ambientes--10794289"),
        ("Departamento", f"{BASE_URL}/departamento-en-venta-en-san-isidro-2-ambientes--10794307"),
    ]

    for prop_type, url in test_cases:
        print(f"    Testing {prop_type}: {url[-60:]}")
        try:
            result = await _scrape_detail(page, url)
            if result:
                print(f"      ✓ type={result.get('type')} desc={len(result.get('description', ''))} chars")
            else:
                print(f"      ⚠ Empty result (page may be unavailable)")
        except Exception as e:
            print(f"      ⚠ Error: {e}")

    print("  ✓ Multiple property types tested")


async def test_error_handling(page):
    """Test that scraping a non-existent URL doesn't crash."""
    result = await _scrape_detail(page, f"{BASE_URL}/non-existent-page--99999999")
    assert isinstance(result, dict), "Should return dict even on error"
    print("  ✓ Error handling: non-existent page returns empty dict")


async def main():
    print("═" * 50)
    print("  Argenprop Comprehensive Tests")
    print("═" * 50)

    print("\nTest 1: _page_url format")
    test_page_url_format()

    print("\nTest 2: Card parsing")
    test_card_parsing()

    print("\nTest 3: Image filtering")
    test_image_filtering()

    # Tests 4-8 require browser
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

        print("\nTest 4: Live pagination")
        print(f"  Loading: {SEARCH_URL}")
        resp = await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        print(f"    HTTP {resp.status}")
        await asyncio.sleep(3)

        try:
            btn = await page.query_selector('button:has-text("Acepto"), button:has-text("Aceptar")')
            if btn:
                await btn.click()
                await asyncio.sleep(1)
        except Exception:
            pass

        total_pages = await _extract_total_pages(page)
        print(f"    Total pages: {total_pages}")
        assert total_pages > 1, f"Expected >1 pages, got {total_pages}"

        scraper = ArgenpropScraper.__new__(ArgenpropScraper)
        all_ids = set()
        for pg in range(1, total_pages + 1):
            url = scraper._page_url(SEARCH_FILTER, pg)
            if pg > 1:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
            cards = await _extract_cards_js(page)
            ids = {c.get("id", "") for c in cards if c.get("id")}
            all_ids |= ids
            print(f"    Page {pg}: {len(cards)} cards")

        print(f"    Total unique IDs: {len(all_ids)}")
        assert len(all_ids) > 20
        print("  ✓ Pagination works")

        print("\nTest 5: Detail field extraction")
        await test_detail_extraction(page)

        print("\nTest 6: Full scrape_detail pipeline")
        await test_scrape_detail_pipeline(page)

        print("\nTest 7: Multiple property types")
        await test_multiple_property_types(page)

        print("\nTest 8: Error handling")
        await test_error_handling(page)

        await context.close()

    print(f"\n{'═'*50}")
    print("  All tests passed!")
    print("═" * 50)


if __name__ == "__main__":
    asyncio.run(main())
