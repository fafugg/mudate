import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

import httpx

from config import settings
from .base import (
    BLOCK_STATUSES,
    BaseScraper,
    UA,
    challenge_marker_in_page,
    coerce_float,
    coerce_int,
    normalize_phone,
    engine_blocked_error,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.remax.com.ar"
API_BASE = "https://api-ar.redremax.com/remaxweb-ar/api"

REMAX_CDN = "https://d1acdg20u0pmxj.cloudfront.net/"

TYPE_MAP = {
    1: "Departamento",
    2: "Departamento",
    3: "Departamento",
    4: "Departamento",
    5: "Departamento",
    9: "Casa",
    10: "Casa",
    11: "Casa",
    12: "PH",
    13: "Terreno",
    14: "Local Comercial",
    15: "Oficina",
    16: "Cochera",
}

PAGE_SIZE = 24
MAX_PAGES = 500
MAX_GALLERY_IMAGES = settings.max_gallery_images


def _remax_image_url(photo: Any) -> Optional[str]:
    """Resolve a Remax photo record to its best URL exposed by the API."""
    is_full_size = False
    if isinstance(photo, str):
        raw = photo
    elif isinstance(photo, dict):
        for key in (
            "originalValue", "originalUrl", "fullValue", "fullUrl",
            "largeValue", "largeUrl",
        ):
            value = photo.get(key)
            if isinstance(value, str) and value.strip():
                raw = value
                is_full_size = True
                break
        else:
            raw = next(
                (
                    photo[key]
                    for key in ("url", "image", "src")
                    if isinstance(photo.get(key), str)
                    and photo[key].strip().startswith(("http://", "https://"))
                ),
                "",
            )
        if not raw:
            for key in ("rawValue", "url", "image", "src"):
                value = photo.get(key)
                if isinstance(value, str) and value.strip():
                    raw = value
                    break
    else:
        return None

    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        return raw

    parts = raw.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "listings":
        if re.fullmatch(r"\d+x\d+", parts[2]):
            return f"{REMAX_CDN}{raw.lstrip('/')}"
        entity_id, image = parts[1], parts[2]
        has_extension = bool(re.search(r"\.[a-z0-9]+(?:\?|$)", image, re.IGNORECASE))
        if not has_extension:
            image += ".webp"
        size = "860x440/" if not is_full_size and not has_extension else ""
        return f"{REMAX_CDN}listings/{entity_id}/{size}{image}"
    return f"{REMAX_CDN}{raw.lstrip('/')}"


def _remax_image_key(url: str) -> str:
    """Treat resized CDN variants of the same photo as one gallery item."""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    parts = [part for part in parts if not re.fullmatch(r"\d+x\d+", part)]
    return f"{parsed.netloc.lower()}/{'/'.join(parts)}".lower()


def _remax_image_score(url: str) -> int:
    """Rank known image variants, preferring explicit originals and larger sizes."""
    lowered = url.lower()
    if any(token in lowered for token in ("original", "fullsize", "full-size")):
        return 10**12
    if any(token in lowered for token in ("thumbnail", "thumb", "preview", "small")):
        return -1
    parsed = urlparse(url)
    for part in parsed.path.split("/"):
        match = re.fullmatch(r"(\d+)x(\d+)", part)
        if match:
            return int(match.group(1)) * int(match.group(2))
    return 10**9


def _extract_remax_images(photos: Any) -> List[str]:
    """Extract unique photo URLs, preferring the best available variant."""
    if not isinstance(photos, list):
        return []

    images: List[str] = []
    positions: Dict[str, int] = {}
    for photo in photos:
        url = _remax_image_url(photo)
        if not url:
            continue
        key = _remax_image_key(url)
        if key in positions:
            position = positions[key]
            if _remax_image_score(url) > _remax_image_score(images[position]):
                images[position] = url
            continue
        positions[key] = len(images)
        images.append(url)
    return images[:MAX_GALLERY_IMAGES]


class RemaxScraper(BaseScraper):
    BASE_URL = BASE_URL

    def _page_url(self, search_filter: str, page: int) -> str:
        """Build paginated URL from the user's search filter.

        Handles four formats:
        - Full URL: https://www.remax.com.ar/listings/buy?...
        - Path with query: /listings/buy?page=0&...
        - Query with leading slash: /?page=0&... (strips /, prepends /listings/buy)
        - Query only: ?page=0&... (prepends /listings/buy)
        """
        # Normalize: strip leading / if followed by ? (query string stored in DB)
        if search_filter.startswith("/?"):
            search_filter = search_filter[1:]  # Remove leading /

        # If search_filter already starts with http, use it directly
        if search_filter.startswith("http"):
            base_url = search_filter
        elif search_filter.startswith("/"):
            base_url = f"{BASE_URL}{search_filter}"
        else:
            # Query-only filter (starts with ?) — prepend /listings/buy
            base_url = f"{BASE_URL}/listings/buy{search_filter}"

        # Replace or inject page parameter
        if "page=" in base_url:
            base_url = re.sub(r"page=\d+", f"page={page}", base_url)
        else:
            sep = "&" if "?" in base_url else "?"
            base_url = f"{base_url}{sep}page={page}"
        return base_url

    async def scrape_search(
        self,
        search_filter: str,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        existing_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen_ids: set = set()
        total_items = 0

        async with self.launch_browser() as page:
            current_page = 0
            total_pages = 1

            while current_page < total_pages and current_page < MAX_PAGES:
                if cancel_check and cancel_check():
                    break

                url = self._page_url(search_filter, current_page)
                if progress_callback:
                    progress_callback(
                        f"Página {current_page + 1}/{total_pages} — {len(results)}/{total_items} propiedades",
                        len(results),
                        total_items,
                    )

                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                # Same contract as Argenprop: refused with nothing collected →
                # surface it in the run banner; refused mid-pagination → keep
                # the already-collected cards and let the 50%-safeguard and the
                # next run recover the rest.
                status = resp.status if resp else None
                if status in BLOCK_STATUSES:
                    if not results:
                        raise engine_blocked_error("Remax", f"HTTP {status}")
                    logger.warning("Remax returned %s mid-pagination on %s", status, url)
                    break

                marker = await challenge_marker_in_page(page)
                if marker:
                    if not results:
                        raise engine_blocked_error(
                            "Remax", f"desafío de Cloudflare ({marker})"
                        )
                    logger.warning("Remax served a challenge mid-pagination on %s", url)
                    break

                # Accept cookies if present
                try:
                    btn = await page.query_selector(
                        'button:has-text("Acepto"), button:has-text("Aceptar"), button:has-text("OK")'
                    )
                    if btn:
                        await btn.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

                # Extract ng-state JSON
                ng_state = await page.evaluate(
                    """() => {
                        const el = document.getElementById('ng-state');
                        if (!el) return null;
                        try { return JSON.parse(el.textContent); } catch(e) { return null; }
                    }"""
                )

                card_listings, total_pages, total_items = self._parse_ng_state(ng_state)

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

                    # Always fetch detail pages to get full galleries
                    entity_id = card.get("_entity_id")
                    if entity_id:
                        detail = await _fetch_detail_api(entity_id)
                        for key, value in detail.items():
                            if value is None:
                                continue
                            if key == "images" and isinstance(value, list):
                                card[key] = _extract_remax_images(
                                    [*(card.get(key) or []), *value]
                                )
                            else:
                                card[key] = value

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

        self.last_paging_info = {"total": total_items, "totalPages": total_pages} if total_items else {}
        return results

    def _parse_ng_state(self, ng_state: Any):
        """Parse the ng-state JSON to extract listings."""
        try:
            if not ng_state or not isinstance(ng_state, dict):
                return [], 1, 0

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
                return [], 1, 0

            data_list = listings_data.get("data", [])
            total_items = listings_data.get("totalItems", 0)
            total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE) if total_items else 1

            listings = []
            for item in data_list:
                parsed = _parse_remax_listing(item)
                if parsed and parsed.get("url"):
                    listings.append(parsed)

            return listings, total_pages, total_items
        except Exception as e:
            logger.error("REMAX ng-state parse error: %s: %s", type(e).__name__, e)
            return [], 1, 0


def _parse_remax_listing(item: dict) -> Optional[Dict[str, Any]]:
    try:
        item_id = item.get("id")
        entity_id = item.get("entityId", "")
        slug = item.get("slug", "")

        se_id = str(item_id) if item_id else ""

        # Build detail URL - use /listings/{slug} format (not /propiedad/{entity_id}/{slug})
        url = f"{BASE_URL}/listings/{slug}" if slug else ""

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
        land_m2 = coerce_float(item.get("dimensionLand"))

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

        images = _extract_remax_images(item.get("photos")) or None

        # Lat/Lng
        location = item.get("location", {})
        lat = None
        lng = None
        if isinstance(location, dict) and location.get("type") == "Point":
            coords = location.get("coordinates", [])
            if len(coords) == 2:
                lng, lat = coords[0], coords[1]

        # Title
        title = item.get("title", "")

        return {
            "search_engine_id": se_id,
            "_entity_id": entity_id,
            "type": prop_type,
            "title": title,
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
            if toilets is not None:
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
                        # Remax API uses 'value' for the feature name
                        name = f.get("value") or f.get("name") or f.get("label") or ""
                    elif isinstance(f, str):
                        name = f
                    else:
                        continue
                    if name and len(name) < 80:
                        amenities.append(name)
                if amenities:
                    result["amenities"] = amenities

            # Property condition (API returns a list of condition objects)
            conditions = listing.get("conditions")
            if isinstance(conditions, list) and conditions:
                cond_obj = conditions[0]
                if isinstance(cond_obj, dict):
                    cond_value = cond_obj.get("value", "")
                    if cond_value:
                        # Map Spanish condition values
                        cond_map = {
                            "muy_bueno": "Muy buen estado",
                            "bueno": "Buen estado",
                            "excelente": "Excelente estado",
                            "regular": "Regular",
                            "nuevo": "A estrenar",
                        }
                        result["condition"] = cond_map.get(cond_value, cond_value)

            # Photos from the detail API are authoritative; retain every distinct photo.
            photos = listing.get("photos", []) or listing.get("images", []) or []
            images = _extract_remax_images(photos)
            if images:
                result["images"] = images

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


def _find_remax_entity_id(
    state: Any, search_engine_id: Optional[str], url: str
) -> Optional[str]:
    """Find a listing's API entity ID in a detail page's serialized ng-state."""
    target_id = str(search_engine_id or "")
    target_slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    matches: List[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            entity_id = value.get("entityId")
            if entity_id:
                item_id = str(value.get("id") or "")
                slug = str(value.get("slug") or "")
                item_slug = (
                    urlparse(str(value.get("url") or "")).path.rstrip("/").rsplit("/", 1)[-1]
                )
                if (
                    (target_id and item_id == target_id)
                    or (target_slug and slug == target_slug)
                    or (target_slug and item_slug == target_slug)
                ):
                    matches.append(str(entity_id))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(state)
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


async def _resolve_remax_entity_id(page, url: str, search_engine_id: Optional[str]) -> Optional[str]:
    """Resolve the API entity ID for legacy records that predate its persistence."""
    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    status = response.status if response else None
    if status in BLOCK_STATUSES:
        raise engine_blocked_error("Remax", f"HTTP {status}")
    marker = await challenge_marker_in_page(page)
    if marker:
        raise engine_blocked_error("Remax", f"desafío de Cloudflare ({marker})")

    state = await page.evaluate("""() => {
        const el = document.getElementById('ng-state');
        if (!el) return null;
        try { return JSON.parse(el.textContent); } catch (e) { return null; }
    }""")
    entity_id = _find_remax_entity_id(state, search_engine_id, url)
    if not entity_id:
        logger.info("Remax entity ID not found in detail page state: %s", url[-80:])
    return entity_id


async def _resolve_remax_entity_ids_from_searches(
    page,
    search_filters: List[str],
    target_ids: Set[str],
    *,
    max_pages: int = 20,
    delay: float = 0.5,
) -> Dict[str, str]:
    """Resolve legacy entity IDs from saved searches, within a bounded page budget."""
    scraper = RemaxScraper()
    resolved: Dict[str, str] = {}
    for search_filter in dict.fromkeys(search_filters):
        if target_ids.issubset(resolved):
            break
        total_pages = 1
        for page_index in range(min(MAX_PAGES, max_pages)):
            response = await page.goto(
                scraper._page_url(search_filter, page_index),
                wait_until="domcontentloaded",
                timeout=30000,
            )
            status = response.status if response else None
            if status in BLOCK_STATUSES:
                raise engine_blocked_error("Remax", f"HTTP {status}")
            marker = await challenge_marker_in_page(page)
            if marker:
                raise engine_blocked_error("Remax", f"desafío de Cloudflare ({marker})")

            state = await page.evaluate("""() => {
                const el = document.getElementById('ng-state');
                if (!el) return null;
                try { return JSON.parse(el.textContent); } catch (e) { return null; }
            }""")
            listings, total_pages, _ = scraper._parse_ng_state(state)
            if not listings:
                break
            for listing in listings:
                listing_id = str(listing.get("search_engine_id") or "")
                entity_id = listing.get("_entity_id")
                if listing_id in target_ids and entity_id:
                    resolved[listing_id] = str(entity_id)
            if target_ids.issubset(resolved) or page_index + 1 >= total_pages:
                break
            if delay:
                await asyncio.sleep(delay)
    return resolved
