import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

import httpx

from .base import BaseScraper, UA, coerce_float, coerce_int, normalize_phone

logger = logging.getLogger(__name__)

BASE_URL = "https://www.remax.com.ar"
API_BASE = "https://api-ar.redremax.com/remaxweb-ar/api"

REMAX_CDN = "https://d1acdg20u0pmxj.cloudfront.net/"

TYPE_MAP = {
    9: "Casa",
    10: "Casa",
    11: "Casa",
    12: "PH",
}

PAGE_SIZE = 24


class RemaxScraper(BaseScraper):
    BASE_URL = BASE_URL

    def _page_url(self, search_filter: str, page: int) -> str:
        base = f"{BASE_URL}/listings/buy"
        filter_str = search_filter.lstrip("?")
        if "page=" in filter_str:
            filter_str = re.sub(r"page=\d+", f"page={page}", filter_str)
        else:
            sep = "&" if "?" in filter_str else "?"
            filter_str = f"{filter_str}{sep}page={page}"
        return f"{base}?{filter_str}"

    async def scrape_search(
        self,
        search_filter: str,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        existing_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen_ids: set = set()

        async with self.launch_browser() as page:
            current_page = 0
            total_pages = 1

            while current_page < total_pages:
                if cancel_check and cancel_check():
                    break

                url = self._page_url(search_filter, current_page)
                if progress_callback:
                    progress_callback(
                        f"Cargando página {current_page + 1}/{total_pages} — {len(results)} propiedades",
                        len(results),
                        len(results),
                    )

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                # Extract ng-state JSON
                ng_state = await page.evaluate(
                    """() => {
                        const el = document.getElementById('ng-state');
                        if (!el) return null;
                        try { return JSON.parse(el.textContent); } catch(e) { return null; }
                    }"""
                )

                card_listings, total_pages = self._parse_ng_state(ng_state)

                if not card_listings:
                    break

                added = 0
                for card in card_listings:
                    se_id = card.get("search_engine_id") or ""
                    if se_id in seen_ids:
                        continue
                    seen_ids.add(se_id)

                    is_known = bool(existing_ids and se_id and se_id in existing_ids)
                    if progress_callback:
                        action = "Verificando precio" if is_known else "Descargando detalle"
                        progress_callback(
                            f"Pág {current_page + 1}/{total_pages} — {action} {added + 1}/{len(card_listings)}",
                            len(results) + added,
                            len(results) + len(card_listings),
                        )

                    if not is_known:
                        entity_id = card.get("_entity_id")
                        if entity_id:
                            detail = await _fetch_detail_api(entity_id)
                            card.update({k: v for k, v in detail.items() if v is not None})

                    card["price_per_m2"] = self.compute_price_per_m2(
                        card.get("price"), card.get("covered_m2") or card.get("total_m2")
                    )
                    results.append(card)
                    added += 1

                    if not is_known:
                        await asyncio.sleep(0.5)

                if added == 0:
                    break

                current_page += 1
                await asyncio.sleep(self.delay)

        return results

    def _parse_ng_state(self, ng_state: Any):
        """Parse the ng-state JSON to extract listings."""
        try:
            if not ng_state or not isinstance(ng_state, dict):
                return [], 1

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
                        break

            if not listings_data:
                return [], 1

            data_list = listings_data.get("data", [])
            total_items = listings_data.get("totalItems", 0)
            total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE) if total_items else 1

            listings = []
            for item in data_list:
                parsed = _parse_remax_listing(item)
                if parsed and parsed.get("url"):
                    listings.append(parsed)

            return listings, total_pages
        except Exception:
            return [], 1


def _parse_remax_listing(item: dict) -> Optional[Dict[str, Any]]:
    try:
        item_id = item.get("id")
        entity_id = item.get("entityId", "")
        slug = item.get("slug", "")

        se_id = str(item_id) if item_id else ""

        # Build detail URL
        url = f"{BASE_URL}/propiedad/{entity_id}/{slug}" if entity_id and slug else ""

        # Price
        price = item.get("price")
        if isinstance(price, str):
            price = coerce_float(price)

        currency_obj = item.get("currency", {})
        currency = currency_obj.get("value", "USD") if isinstance(currency_obj, dict) else "USD"
        currency = currency.upper()
        if currency not in ("USD", "ARS"):
            currency = "USD"

        # Rooms
        ambientes = item.get("totalRooms")
        if isinstance(ambientes, str):
            ambientes = coerce_int(ambientes)

        dormitorios = item.get("bedrooms")
        if isinstance(dormitorios, str):
            dormitorios = coerce_int(dormitorios)

        banos = item.get("bathrooms")
        if isinstance(banos, str):
            banos = coerce_int(banos)

        # M2
        covered_m2 = coerce_float(item.get("dimensionCovered"))
        total_m2 = coerce_float(item.get("dimensionTotalBuilt"))

        # Expenses
        expenses = coerce_float(item.get("expensesPrice"))
        exp_curr_obj = item.get("expensesCurrency", {})
        expenses_currency = exp_curr_obj.get("value", "ARS") if isinstance(exp_curr_obj, dict) else "ARS"

        # Address
        display_address = item.get("displayAddress", "")
        geo_label = item.get("geoLabel", "")
        address = display_address or geo_label or None

        # Property type
        type_obj = item.get("type", {})
        type_id = type_obj.get("id") if isinstance(type_obj, dict) else None
        prop_type = TYPE_MAP.get(type_id, type_obj.get("value", "") if isinstance(type_obj, dict) else "")
        prop_type = prop_type or None

        # Real estate / agent
        associate = item.get("associate", {})
        real_estate = None
        if isinstance(associate, dict):
            real_estate = associate.get("name")
            if not real_estate:
                office = associate.get("office", {})
                if isinstance(office, dict):
                    real_estate = office.get("name")

        # Phone
        phone = None
        if isinstance(associate, dict):
            raw_phone = associate.get("phone") or ""
            if raw_phone:
                phone = normalize_phone(raw_phone)

        # Published date
        published_at = None
        created = item.get("createdAt")
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                published_at = dt.strftime("%Y-%m-%d")
            except Exception:
                published_at = str(created)[:10]

        # Images
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
        images = images[:40] if images else None

        # Lat/Lng
        location = item.get("location", {})
        lat = None
        lng = None
        if isinstance(location, dict) and location.get("type") == "Point":
            coords = location.get("coordinates", [])
            if len(coords) == 2:
                lng, lat = coords[0], coords[1]

        return {
            "search_engine_id": se_id,
            "_entity_id": entity_id,
            "type": prop_type,
            "ambientes": ambientes,
            "dormitorios": dormitorios,
            "banos": banos,
            "price": price,
            "currency": currency,
            "address": address,
            "covered_m2": covered_m2,
            "total_m2": total_m2,
            "expenses": expenses if expenses and expenses > 0 else None,
            "expenses_currency": expenses_currency if expenses and expenses > 0 else None,
            "real_estate": real_estate,
            "real_estate_phone": phone,
            "published_at": published_at,
            "url": url,
            "images": images,
            "lat": lat,
            "lng": lng,
        }
    except Exception:
        return None


async def _fetch_detail_api(entity_id: str) -> Dict[str, Any]:
    """Fetch listing details from the Remax API."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{API_BASE}/listings/findById/{entity_id}",
                headers={"User-Agent": UA},
            )
            if resp.status_code != 200:
                return {}

            data = resp.json()
            listing = data.get("data")
            if not listing or not isinstance(listing, dict):
                return {}

            result: Dict[str, Any] = {}

            # Description
            desc = listing.get("description", "")
            if desc:
                result["description"] = desc[:2000]

            # Year built → age_years
            year_built = listing.get("yearBuilt")
            if year_built:
                try:
                    result["age_years"] = datetime.now().year - int(year_built)
                except (ValueError, TypeError):
                    pass

            # Floors
            floors = listing.get("floors")
            if floors:
                result["floor"] = str(floors)

            # Parking
            parking = listing.get("parkingSpaces")
            if parking is not None:
                result["parking"] = int(parking) > 0

            # Toilets
            toilets = listing.get("toilets")
            if toilets:
                result["toilettes"] = int(toilets)

            # Expenses
            exp_price = listing.get("expensesPrice")
            if exp_price and float(exp_price) > 0:
                result["expenses"] = float(exp_price)
                exp_curr = listing.get("expensesCurrency", {})
                result["expenses_currency"] = exp_curr.get("value", "ARS") if isinstance(exp_curr, dict) else "ARS"

            # Features/amenities
            features = listing.get("features", [])
            if isinstance(features, list):
                amenities = []
                for f in features:
                    if isinstance(f, dict):
                        name = f.get("name") or f.get("label") or f.get("value") or ""
                    elif isinstance(f, str):
                        name = f
                    else:
                        continue
                    if name and len(name) < 80:
                        amenities.append(name)
                if amenities:
                    result["amenities"] = amenities

            # Photos (full resolution from detail API)
            photos = listing.get("photos", [])
            if isinstance(photos, list) and photos:
                images = []
                for photo in photos:
                    if isinstance(photo, dict):
                        raw = photo.get("rawValue", "")
                        if raw:
                            if raw.startswith("http"):
                                images.append(raw)
                            else:
                                images.append(f"{REMAX_CDN}{raw}")
                if images:
                    result["images"] = images[:40]

            # Agent phone
            associate = listing.get("associate", {})
            if isinstance(associate, dict):
                phones = associate.get("phones", [])
                if isinstance(phones, list) and phones:
                    for phone_obj in phones:
                        if isinstance(phone_obj, dict) and phone_obj.get("primary"):
                            raw_phone = phone_obj.get("value", "")
                            if raw_phone:
                                result["real_estate_phone"] = normalize_phone(raw_phone)
                                break

            return result
    except Exception as e:
        logger.error("REMAX API error %s: %s: %s", entity_id, type(e).__name__, e)
        return {}
