"""
Diagnostic test for the MercadoLibre scraper.

Tests JSON-LD extraction from listing pages, detail page extraction, image extraction,
and URL construction against a live MercadoLibre search.

Usage (from backend/):
    python tests/test_mercadolibre.py
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright

SEARCH_URL = "https://inmuebles.mercadolibre.com.ar/3-dormitorios/bsas-gba-norte/san-isidro/_PriceRange_350000USD-360000USD_NoIndex_True"
BASE_URL = "https://inmuebles.mercadolibre.com.ar"
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

        # ── Step 2: Extract JSON-LD ────────────────────────────────────────
        print("\n--- Extracting JSON-LD ---")
        html = await page.content()

        # Find all JSON-LD script blocks
        ld_blocks = re.findall(
            r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.S,
        )
        print(f"  Found {len(ld_blocks)} JSON-LD blocks")

        all_listings = []
        for i, block in enumerate(ld_blocks):
            try:
                data = json.loads(block)
                if isinstance(data, dict):
                    # Check for @graph
                    graph = data.get("@graph", [])
                    if graph:
                        print(f"  Block {i}: @graph with {len(graph)} items")
                        for item in graph:
                            if item.get("@type") == "RealEstateListing":
                                all_listings.append(item)
                                print(f"    Found RealEstateListing: {item.get('name', 'unnamed')[:60]}")
                    elif data.get("@type") == "RealEstateListing":
                        all_listings.append(data)
                        print(f"  Block {i}: Direct RealEstateListing")
                    else:
                        print(f"  Block {i}: @type={data.get('@type', 'none')}")
                elif isinstance(data, list):
                    print(f"  Block {i}: Array with {len(data)} items")
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "RealEstateListing":
                            all_listings.append(item)
            except json.JSONDecodeError as e:
                print(f"  Block {i}: JSON parse error: {e}")

        print(f"\n  Total RealEstateListing items: {len(all_listings)}")

        if not all_listings:
            print("  No listings found in JSON-LD")
            print(f"  Page title: {await page.title()}")
            print(f"  Page URL: {page.url}")
            await context.close()
            return

        # ── Step 3: Parse first 3 listings ─────────────────────────────────
        print(f"\n--- Parsing {min(3, len(all_listings))} card listings ---")
        cards = []
        for i, item in enumerate(all_listings[:3]):
            print(f"\n  Card {i+1}:")
            print(f"    Keys: {list(item.keys())}")

            offers = item.get("offers", {})
            price = offers.get("price")
            currency = (offers.get("priceCurrency") or "USD").upper()
            url = offers.get("url", "")
            se_id = ""
            m = re.search(r"(MLA-\d+)", url)
            if m:
                se_id = m.group(1)

            addr = item.get("address", {})
            locality = addr.get("addressLocality", "")
            region = addr.get("addressRegion", "")
            address = ", ".join(filter(None, [locality, region]))

            floor_size = item.get("floorSize", {})
            total_m2 = floor_size.get("value") if floor_size.get("unitCode") == "MTK" else None

            # Check additionalProperty
            additional = item.get("additionalProperty", [])
            print(f"    additionalProperty: {len(additional)} items")
            if additional:
                for prop in additional[:5]:
                    print(f"      {prop.get('name', '?')}: {prop.get('value', '?')}")

            print(f"    se_id: {se_id}")
            print(f"    price: {price} {currency}")
            print(f"    address: {address}")
            print(f"    total_m2: {total_m2}")
            print(f"    url: {url[:80]}")
            print(f"    @type: {item.get('@type')}")
            print(f"    name: {item.get('name', '')[:60]}")

            cards.append({"se_id": se_id, "url": url, "raw": item})

        # ── Step 4: Extract detail pages ───────────────────────────────────
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
                await asyncio.sleep(3)

                # Extract JSON-LD from detail page
                detail_html = await page.content()
                detail_ld_blocks = re.findall(
                    r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>',
                    detail_html,
                    re.S,
                )

                for block in detail_ld_blocks:
                    try:
                        data = json.loads(block)
                        if isinstance(data, dict):
                            graph = data.get("@graph", [])
                            for item in graph:
                                if item.get("@type") in ("Product", "RealEstateListing"):
                                    print(f"    Found detail @type: {item.get('@type')}")
                                    print(f"    Keys: {list(item.keys())}")

                                    # Images
                                    images = item.get("image", [])
                                    if isinstance(images, str):
                                        images = [images]
                                    print(f"    images: {len(images)} found")
                                    if images:
                                        print(f"    first image: {str(images[0])[:100]}")

                                    # Amenities from additionalProperty
                                    amenities = item.get("additionalProperty", [])
                                    print(f"    additionalProperty: {len(amenities)} items")
                                    for prop in amenities[:5]:
                                        print(f"      {prop.get('name', '?')}: {prop.get('value', '?')}")

                                    # Description
                                    desc = item.get("description", "")
                                    print(f"    description: {len(desc)} chars")

                                    # Address
                                    addr = item.get("address", {})
                                    print(f"    address: {addr}")

                                    _table(f"Detail JSON-LD: {url[-60:]}", item)
                    except json.JSONDecodeError:
                        pass

                # DOM fallback: check for images
                img_count = await page.evaluate("""() => {
                    const imgs = document.querySelectorAll('img[src*="http"]');
                    return [...imgs].filter(i => !i.src.endsWith('.svg') && (
                        i.src.includes('cdn') || i.src.includes('mlstatic') || i.src.includes('image')
                    )).length;
                }""")
                print(f"    DOM CDN images: {img_count}")

            except Exception as e:
                print(f"    ERROR: {type(e).__name__}: {e}")

        await context.close()

    print(f"\n{'═'*72}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
