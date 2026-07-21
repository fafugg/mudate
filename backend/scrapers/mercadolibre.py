import asyncio
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Set

from .base import BaseScraper, UA, coerce_float, coerce_int

logger = logging.getLogger(__name__)

BASE_URL = "https://inmuebles.mercadolibre.com.ar"


class MercadoLibreScraper(BaseScraper):
    BASE_URL = BASE_URL

    def _page_url(self, search_filter: str, offset: int) -> str:
        base = f"{BASE_URL}{search_filter}"
        if offset <= 0:
            return base
        sep = "&" if "?" in search_filter else "?"
        return f"{base}{sep}_Desde_{offset}_NoIndex_True"

    async def scrape_search(
        self,
        search_filter: str,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        existing_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen_ids: set = set()
        PAGE_SIZE = 48

        async with self.launch_browser() as page:
            current_offset = 0
            has_more = True

            while has_more:
                if cancel_check and cancel_check():
                    break

                url = self._page_url(search_filter, current_offset)
                page_num = (current_offset // PAGE_SIZE) + 1
                if progress_callback:
                    progress_callback(
                        f"Cargando página {page_num} — {len(results)} propiedades",
                        len(results),
                        len(results),
                    )

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)

                card_listings = self._extract_from_json_ld(await page.content())

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
                            f"Pág {page_num} — {action} {added + 1}/{len(card_listings)}",
                            len(results) + added,
                            len(results) + len(card_listings),
                        )

                    if not is_known and card.get("url"):
                        detail = await _scrape_detail(page, card["url"])
                        card.update({k: v for k, v in detail.items() if v is not None})

                    card["price_per_m2"] = self.compute_price_per_m2(
                        card.get("price"), card.get("covered_m2") or card.get("total_m2")
                    )
                    results.append(card)
                    added += 1

                    if not is_known:
                        await asyncio.sleep(1.5)

                if added == 0:
                    break

                current_offset += PAGE_SIZE
                has_more = len(card_listings) == PAGE_SIZE
                await asyncio.sleep(self.delay)

        return results

    def _extract_from_json_ld(self, html: str) -> List[Dict[str, Any]]:
        """Extract listings from the JSON-LD script tag."""
        try:
            m = re.search(
                r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>',
                html,
                re.S,
            )
            if not m:
                return []

            data = json.loads(m.group(1))
            graph = data.get("@graph", []) if isinstance(data, dict) else []

            listings = []
            for item in graph:
                if item.get("@type") != "RealEstateListing":
                    continue
                parsed = _parse_json_ld_listing(item)
                if parsed and parsed.get("url"):
                    listings.append(parsed)
            return listings
        except Exception:
            return []


def _parse_json_ld_listing(item: dict) -> Optional[Dict[str, Any]]:
    try:
        offers = item.get("offers", {})
        price = offers.get("price")
        currency = (offers.get("priceCurrency") or "USD").upper()
        if currency not in ("USD", "ARS"):
            currency = "USD"

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
        total_m2 = coerce_float(floor_size.get("value")) if floor_size.get("unitCode") == "MTK" else None

        seller = item.get("seller", {})
        real_estate = seller.get("name") if isinstance(seller, dict) else None

        published_at = item.get("datePosted")

        image = item.get("image", "")
        images = [image] if image and image.startswith("http") else []

        return {
            "search_engine_id": se_id,
            "type": None,
            "ambientes": coerce_int(item.get("numberOfRooms")),
            "dormitorios": None,
            "banos": None,
            "price": coerce_float(price),
            "currency": currency,
            "address": address or None,
            "covered_m2": total_m2,
            "total_m2": total_m2,
            "real_estate": real_estate,
            "published_at": published_at,
            "url": url,
            "images": images or None,
        }
    except Exception:
        return None


async def _scrape_detail(page, url: str) -> Dict[str, Any]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        result: Dict[str, Any] = {}

        # Extract JSON-LD from detail page
        ld_data = await page.evaluate(
            """() => {
                const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                for (const s of scripts) {
                    try {
                        const d = JSON.parse(s.textContent);
                        if (d['@type'] === 'Product' || d['@type'] === 'RealEstateListing' || (d['@graph'] && d['@graph'].length)) return d;
                    } catch(e) {}
                }
                return null;
            }"""
        )

        if ld_data:
            detail = _parse_detail_ld(ld_data)
            result.update({k: v for k, v in detail.items() if v is not None})

        # DOM fallback for fields not in JSON-LD
        result["description"] = await _safe_text(page, "[class*='description'], [class*='descripcion'], [id*='description']") or result.get("description")
        result["amenities"] = await _safe_texts(page, "[class*='amenity'], [class*='attribute'], [class*='feature']") or result.get("amenities")

        if not result.get("images"):
            imgs = await page.evaluate("""() => {
                return [...document.querySelectorAll('img[src*="http"]')]
                    .map(i => i.src)
                    .filter(s => s && !s.endsWith('.svg') && (s.includes('cdn') || s.includes('mlstatic') || s.includes('image')))
                    .slice(0, 40);
            }""")
            if imgs:
                result["images"] = imgs

        return result
    except Exception as e:
        logger.error("ML detail error %s: %s: %s", url[-80:], type(e).__name__, e)
        return {}


def _parse_detail_ld(data: dict) -> Dict[str, Any]:
    try:
        if "@graph" in data:
            for item in data["@graph"]:
                if item.get("@type") in ("Product", "RealEstateListing"):
                    data = item
                    break

        result: Dict[str, Any] = {}

        desc = data.get("description", "")
        if desc:
            result["description"] = desc[:2000]

        # Amenities from additionalProperty or similar
        amenities = []
        for prop in data.get("additionalProperty", []):
            name = prop.get("name") or prop.get("value") or ""
            if name:
                amenities.append(name)
        if amenities:
            result["amenities"] = amenities

        # Images
        imgs = []
        img_data = data.get("image", [])
        if isinstance(img_data, str):
            img_data = [img_data]
        if isinstance(img_data, list):
            for img in img_data:
                if isinstance(img, str) and img.startswith("http"):
                    imgs.append(img)
                elif isinstance(img, dict):
                    u = img.get("url", "")
                    if u.startswith("http"):
                        imgs.append(u)
        if imgs:
            result["images"] = imgs[:40]

        return result
    except Exception:
        return {}


async def _safe_text(page, selector: str) -> Optional[str]:
    try:
        el = await page.query_selector(selector)
        if el:
            text = (await el.inner_text()).strip()
            return text if text else None
    except Exception:
        pass
    return None


async def _safe_texts(page, selector: str) -> Optional[List[str]]:
    try:
        els = await page.query_selector_all(selector)
        texts = []
        for el in els:
            t = (await el.inner_text()).strip()
            if t and len(t) < 80:
                texts.append(t)
        return texts if texts else None
    except Exception:
        pass
    return None
