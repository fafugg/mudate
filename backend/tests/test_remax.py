"""
Diagnostic test for the Remax scraper.

Tests ng-state extraction from listing pages, API detail fetch, URL construction,
and type mapping against a live Remax search.

Usage (from backend/):
    python tests/test_remax.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from playwright.async_api import async_playwright

SEARCH_URL = "https://www.remax.com.ar/listings/buy?page=0&pageSize=24&sort=-createdAt&in:operationId=1&in:typeId=9,10,11&pricein=1:350000:360000&locations=in:::116@%3Cb%3ESan%3C%2Fb%3E%20%3Cb%3EIsidro%3C%2Fb%3E::::&landingPath=&filterCount=2&viewMode=listViewMode"
BASE_URL = "https://www.remax.com.ar"
API_BASE = "https://api-ar.redremax.com/remaxweb-ar/api"
REMAX_CDN = "https://d1acdg20u0pmxj.cloudfront.net/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

TYPE_MAP = {
    9: "Casa",
    10: "Casa",
    11: "Casa",
    12: "PH",
}


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


def _parse_remax_listing(item: dict) -> dict:
    """Parse a single Remax listing from ng-state data."""
    item_id = item.get("id")
    entity_id = item.get("entityId", "")
    slug = item.get("slug", "")
    se_id = str(item_id) if item_id else ""
    url = f"{BASE_URL}/propiedad/{entity_id}/{slug}" if entity_id and slug else ""

    price = item.get("price")
    if isinstance(price, str):
        try:
            price = float(price.replace(".", "").replace(",", "."))
        except (ValueError, TypeError):
            price = None

    currency_obj = item.get("currency", {})
    currency = currency_obj.get("value", "USD") if isinstance(currency_obj, dict) else "USD"
    currency = currency.upper()

    ambientes = item.get("totalRooms")
    dormitorios = item.get("bedrooms")
    banos = item.get("bathrooms")
    covered_m2 = item.get("dimensionCovered")
    total_m2 = item.get("dimensionTotalBuilt")
    expenses = item.get("expensesPrice")

    display_address = item.get("displayAddress", "")
    geo_label = item.get("geoLabel", "")
    address = display_address or geo_label or None

    type_obj = item.get("type", {})
    type_id = type_obj.get("id") if isinstance(type_obj, dict) else None
    prop_type = TYPE_MAP.get(type_id, type_obj.get("value", "") if isinstance(type_obj, dict) else "")

    associate = item.get("associate", {})
    real_estate = None
    if isinstance(associate, dict):
        real_estate = associate.get("name")
        if not real_estate:
            office = associate.get("office", {})
            if isinstance(office, dict):
                real_estate = office.get("name")

    images = []
    photos = item.get("photos", [])
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, dict):
                raw = photo.get("rawValue", "")
                if raw:
                    if raw.startswith("http"):
                        images.append(raw)
                    else:
                        images.append(f"{REMAX_CDN}{raw}")

    location = item.get("location", {})
    lat = None
    lng = None
    if isinstance(location, dict) and location.get("type") == "Point":
        coords = location.get("coordinates", [])
        if len(coords) == 2:
            lng, lat = coords[0], coords[1]

    return {
        "se_id": se_id,
        "entity_id": entity_id,
        "url": url,
        "price": price,
        "currency": currency,
        "ambientes": ambientes,
        "dormitorios": dormitorios,
        "banos": banos,
        "covered_m2": covered_m2,
        "total_m2": total_m2,
        "expenses": expenses,
        "address": address,
        "type": prop_type,
        "type_id": type_id,
        "real_estate": real_estate,
        "images": images,
        "lat": lat,
        "lng": lng,
    }


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
        await asyncio.sleep(5)

        # ── Step 2: Extract ng-state JSON ──────────────────────────────────
        print("\n--- Extracting ng-state ---")
        ng_state = await page.evaluate("""() => {
            const el = document.getElementById('ng-state');
            if (!el) return null;
            try { return JSON.parse(el.textContent); } catch(e) { return {error: e.toString()}; }
        }""")

        if not ng_state or isinstance(ng_state, dict) and "error" in ng_state:
            print(f"  ng-state not found or error: {ng_state}")
            print(f"  Page title: {await page.title()}")
            print(f"  Page URL: {page.url}")
            await context.close()
            return

        print(f"  ng-state keys: {list(ng_state.keys())}")

        # ── Step 3: Find listing data in ng-state ─────────────────────────
        print("\n--- Searching for listing data ---")
        listings_data = None
        for key, value in ng_state.items():
            if not isinstance(value, dict):
                continue
            b = value.get("b")
            if not isinstance(b, dict):
                continue
            data = b.get("data")
            if not isinstance(data, dict):
                continue
            data_list = data.get("data")
            if isinstance(data_list, list) and len(data_list) > 0:
                first = data_list[0]
                if isinstance(first, dict) and "id" in first and "price" in first:
                    listings_data = data
                    print(f"  Found listing data in key: {key}")
                    print(f"  Items: {len(data_list)}")
                    print(f"  totalItems: {data.get('totalItems', '?')}")
                    print(f"  First item keys: {list(first.keys())}")
                    break

        if not listings_data:
            print("  No listing data found in ng-state")
            # Show structure for debugging
            for key, value in list(ng_state.items())[:3]:
                print(f"  Key: {key}")
                if isinstance(value, dict):
                    print(f"    Sub-keys: {list(value.keys())[:10]}")
                    b = value.get("b")
                    if isinstance(b, dict):
                        print(f"    b keys: {list(b.keys())[:10]}")
            await context.close()
            return

        data_list = listings_data.get("data", [])

        # ── Step 4: Parse first 3 listings ─────────────────────────────────
        print(f"\n--- Parsing {min(3, len(data_list))} card listings ---")
        cards = []
        for i, item in enumerate(data_list[:3]):
            print(f"\n  Card {i+1}:")
            card = _parse_remax_listing(item)
            for k, v in card.items():
                if v is not None and k != "images":
                    print(f"    {k}: {v}")
            if card["images"]:
                print(f"    images: {len(card['images'])} found")
                print(f"    first image: {card['images'][0][:100]}")
            cards.append(card)

        # ── Step 5: Fetch detail via API ───────────────────────────────────
        print(f"\n--- Fetching detail via API ---")
        for i, card in enumerate(cards[:2]):
            entity_id = card.get("entity_id", "")
            if not entity_id:
                print(f"  Card {i+1}: No entity_id, skipping")
                continue

            print(f"\n  → API call: {API_BASE}/listings/findById/{entity_id}")
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        f"{API_BASE}/listings/findById/{entity_id}",
                        headers={"User-Agent": UA},
                    )
                    print(f"    HTTP status: {resp.status_code}")
                    if resp.status_code == 200:
                        data = resp.json()
                        listing = data.get("data")
                        if listing and isinstance(listing, dict):
                            print(f"    detail keys: {list(listing.keys())}")

                            # Description
                            desc = listing.get("description", "")
                            print(f"    description: {len(desc)} chars")

                            # Features/amenities
                            features = listing.get("features", [])
                            print(f"    features: {len(features)} items")
                            if features:
                                for f in features[:5]:
                                    if isinstance(f, dict):
                                        print(f"      {f.get('name', '?')}: {f.get('label', '?')}")
                                    else:
                                        print(f"      {f}")

                            # Photos
                            photos = listing.get("photos", [])
                            print(f"    photos: {len(photos)} items")

                            # Year built
                            year_built = listing.get("yearBuilt")
                            print(f"    yearBuilt: {year_built}")

                            # Parking
                            parking = listing.get("parkingSpaces")
                            print(f"    parkingSpaces: {parking}")

                            # Floors
                            floors = listing.get("floors")
                            print(f"    floors: {floors}")

                            # Toilets
                            toilets = listing.get("toilets")
                            print(f"    toilets: {toilets}")

                            # Expenses
                            exp_price = listing.get("expensesPrice")
                            print(f"    expensesPrice: {exp_price}")

                            # Associate/phone
                            associate = listing.get("associate", {})
                            if isinstance(associate, dict):
                                print(f"    associate name: {associate.get('name')}")
                                phones = associate.get("phones", [])
                                print(f"    phones: {len(phones)} items")

                            _table(f"API Detail: {entity_id}", listing)
                        else:
                            print(f"    No listing data in response")
                            print(f"    Response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                    else:
                        print(f"    Error: {resp.text[:200]}")
            except Exception as e:
                print(f"    ERROR: {type(e).__name__}: {e}")

        await context.close()

    print(f"\n{'═'*72}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
