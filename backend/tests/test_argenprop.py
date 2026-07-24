"""
Diagnostic test for the Argenprop scraper.

Tests card extraction from __NEXT_DATA__, detail page extraction, image extraction,
and URL construction against a live Argenprop search.

Usage (from backend/):
    python tests/test_argenprop.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright

SEARCH_URL = "https://www.argenprop.com/casas/venta/partido-de-san-isidro/3-dormitorios/dolares-350000-360000?solo-ver-dolares"
BASE_URL = "https://www.argenprop.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, list):
        if not v:
            return "[]"
        joined = " | ".join(str(x) for x in v[:5])
        suffix = f" … (+{len(v)-5} more)" if len(v) > 5 else ""
        return joined + suffix
    if isinstance(v, str) and len(v) > 120:
        return v[:117] + "..."
    return str(v)


def _table(title: str, data: dict):
    print(f"\n{'═'*72}")
    print(f"  {title}")
    print(f"{'═'*72}")
    if not data:
        print("  (no data extracted)")
        return
    kw = max(len(k) for k in data) + 2
    for k, v in data.items():
        print(f"  {k:<{kw}} {_fmt(v)}")


async def main():
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

        # ── Step 1: Load search page ──────────────────────────────────────
        print(f"Loading search: {SEARCH_URL}")
        resp = await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        print(f"  HTTP status: {resp.status}")
        await asyncio.sleep(3)

        # Accept cookies if present
        try:
            btn = await page.query_selector('button:has-text("Acepto"), button:has-text("Aceptar")')
            if btn:
                await btn.click()
                print("  Accepted cookies")
                await asyncio.sleep(1)
        except Exception:
            pass

        # ── Step 2: Extract __NEXT_DATA__ ──────────────────────────────────
        print("\n--- Extracting __NEXT_DATA__ ---")
        next_data = await page.evaluate("""() => {
            try {
                const el = document.getElementById('__NEXT_DATA__');
                return el ? JSON.parse(el.textContent) : null;
            } catch(e) { return {error: e.toString()}; }
        }""")

        if not next_data or isinstance(next_data, dict) and "error" in next_data:
            print(f"  __NEXT_DATA__ not found or error: {next_data}")
            # Try DOM fallback
            print("\n--- Trying DOM fallback ---")
            cards_dom = await page.evaluate("""() => {
                const selectors = ['.card', '.listing__item', '[class*="card"]', 'article'];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 0) return { selector: sel, count: els.length };
                }
                return null;
            }""")
            print(f"  DOM cards: {cards_dom}")

            # Dump page title and URL for debugging
            title = await page.title()
            print(f"  Page title: {title}")
            print(f"  Page URL: {page.url}")
            await context.close()
            return

        # ── Step 3: Parse listings from __NEXT_DATA__ ──────────────────────
        page_props = next_data.get("props", {}).get("pageProps", {})
        listings_raw = (
            page_props.get("listings")
            or page_props.get("properties")
            or page_props.get("results")
            or []
        )
        total = page_props.get("total") or page_props.get("totalCount") or len(listings_raw)
        page_size = page_props.get("pageSize") or 20
        total_pages = max(1, -(-total // page_size)) if total else 1

        print(f"  Total listings: {total}")
        print(f"  Page size: {page_size}")
        print(f"  Total pages: {total_pages}")
        print(f"  Listings on this page: {len(listings_raw)}")

        # Show all keys in pageProps for debugging
        print(f"  pageProps keys: {list(page_props.keys())}")

        if not listings_raw:
            print("  No listings found in __NEXT_DATA__")
            # Show structure for debugging
            print(f"  next_data keys: {list(next_data.keys())}")
            if "props" in next_data:
                print(f"  props keys: {list(next_data['props'].keys())}")
            await context.close()
            return

        # ── Step 4: Parse first 3 card listings ────────────────────────────
        print(f"\n--- Parsing {min(3, len(listings_raw))} card listings ---")
        cards = []
        for i, posting in enumerate(listings_raw[:3]):
            print(f"\n  Card {i+1}:")
            print(f"    Keys: {list(posting.keys()) if isinstance(posting, dict) else type(posting)}")
            if isinstance(posting, dict):
                # Try to extract key fields
                price_obj = posting.get("price") or {}
                raw_price = price_obj.get("amount") or price_obj.get("value") or posting.get("precio")
                address = posting.get("address") or posting.get("direccion") or posting.get("location", {}).get("address") or ""
                url_path = posting.get("url") or posting.get("link") or ""
                se_id = str(posting.get("id") or posting.get("propertyId") or "")

                print(f"    id: {se_id}")
                print(f"    price raw: {raw_price}")
                print(f"    address: {address}")
                print(f"    url_path: {url_path}")

                if url_path and not url_path.startswith("http"):
                    url_path = f"{BASE_URL}{url_path}"
                cards.append({"se_id": se_id, "url": url_path, "raw": posting})

        # ── Step 5: Extract detail pages for first 2 listings ──────────────
        print(f"\n--- Extracting detail pages ---")
        for i, card in enumerate(cards[:2]):
            url = card.get("url", "")
            if not url:
                print(f"  Card {i+1}: No URL, skipping")
                continue

            print(f"\n  → Loading detail: {url[-80:]}")
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                print(f"    HTTP status: {resp.status}")
                await asyncio.sleep(2)

                # Extract __NEXT_DATA__ from detail page
                detail_data = await page.evaluate("""() => {
                    try {
                        const el = document.getElementById('__NEXT_DATA__');
                        return el ? JSON.parse(el.textContent) : null;
                    } catch(e) { return {error: e.toString()}; }
                }""")

                if detail_data and "error" not in detail_data:
                    detail_props = detail_data.get("props", {}).get("pageProps", {})
                    posting = detail_props.get("property") or detail_props.get("posting") or detail_props or {}

                    print(f"    detail pageProps keys: {list(detail_props.keys())}")
                    print(f"    posting keys: {list(posting.keys()) if isinstance(posting, dict) else type(posting)}")

                    # Extract specific fields
                    if isinstance(posting, dict):
                        # Images
                        images = (
                            posting.get("photos")
                            or posting.get("images")
                            or posting.get("gallery")
                            or posting.get("fotos")
                            or []
                        )
                        print(f"    images: {len(images)} found")
                        if images:
                            first = images[0] if isinstance(images[0], str) else str(images[0])[:100]
                            print(f"    first image: {first}")

                        # Amenities
                        amenities = posting.get("amenities") or posting.get("caracteristicas") or []
                        print(f"    amenities: {len(amenities)} found")
                        if amenities:
                            print(f"    first 3: {amenities[:3]}")

                        # Description
                        desc = posting.get("description") or posting.get("descripcion") or ""
                        print(f"    description: {len(desc)} chars")

                        # Expenses
                        expenses = posting.get("expenses") or posting.get("expensas")
                        print(f"    expenses: {expenses}")

                        # Publisher
                        publisher = posting.get("publisher") or posting.get("inmobiliaria") or {}
                        if isinstance(publisher, dict):
                            print(f"    publisher name: {publisher.get('name')}")
                            print(f"    publisher phone: {publisher.get('phone')}")
                        else:
                            print(f"    publisher: {publisher}")

                        # Show all posting fields for debugging
                        _table(f"Detail: {url[-60:]}", {k: v for k, v in posting.items()})
                else:
                    print(f"    __NEXT_DATA__ error or not found: {detail_data}")
            except Exception as e:
                print(f"    ERROR: {type(e).__name__}: {e}")

        await context.close()

    print(f"\n{'═'*72}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
