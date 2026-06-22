import asyncio
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.async_api import async_playwright

from .base import BaseScraper

BASE_URL = "https://www.argenprop.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


class ArgenpropScraper(BaseScraper):
    BASE_URL = BASE_URL

    def _page_url(self, search_filter: str, page: int) -> str:
        """Argenprop uses ?pagina=N query param."""
        base = f"{BASE_URL}{search_filter}"
        if page <= 1:
            return base
        sep = "&" if "?" in search_filter else "?"
        return f"{base}{sep}pagina={page}"

    async def scrape_search(
        self,
        search_filter: str,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        existing_ids: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        results = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 900},
                locale="es-AR",
            )
            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )

            try:
                current_page = 1
                total_pages = 1

                while current_page <= total_pages:
                    url = self._page_url(search_filter, current_page)
                    if progress_callback:
                        progress_callback(
                            f"Cargando página {current_page}/{total_pages}...",
                            len(results),
                            len(results),
                        )

                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)

                    # Try __NEXT_DATA__ first
                    next_data = await page.evaluate(
                        "() => { try { return JSON.parse(document.getElementById('__NEXT_DATA__').textContent); } catch(e) { return null; } }"
                    )

                    card_listings = []
                    if next_data:
                        card_listings, total_pages = _extract_from_next_data(next_data, current_page)

                    if not card_listings:
                        card_listings, total_pages = await _extract_from_dom(page, current_page)

                    if not card_listings:
                        break

                    for i, card in enumerate(card_listings):
                        if cancel_check and cancel_check():
                            break
                        se_id    = card.get("search_engine_id") or ""
                        is_known = bool(existing_ids and se_id and se_id in existing_ids)
                        if progress_callback:
                            action = "Verificando precio" if is_known else "Descargando detalle"
                            progress_callback(
                                f"Pág {current_page}/{total_pages} — {action} {i+1}/{len(card_listings)}",
                                len(results) + i,
                                len(results) + len(card_listings),
                            )

                        if not is_known and card.get("url"):
                            detail = await _scrape_detail(page, card["url"])
                            card.update({k: v for k, v in detail.items() if v is not None})

                        card["price_per_m2"] = self.compute_price_per_m2(
                            card.get("price"), card.get("covered_m2") or card.get("total_m2")
                        )
                        results.append(card)
                        if not is_known:
                            await asyncio.sleep(1.5)

                    current_page += 1
                    await asyncio.sleep(self.delay)

            finally:
                await browser.close()

        return results


# ── Next.js extraction ──────────────────────────────────────────────────────

def _extract_from_next_data(data: dict, current_page: int):
    try:
        page_props = data.get("props", {}).get("pageProps", {})
        listings_raw = (
            page_props.get("listings")
            or page_props.get("properties")
            or page_props.get("results")
            or []
        )
        total = page_props.get("total") or page_props.get("totalCount") or len(listings_raw)
        page_size = page_props.get("pageSize") or 20
        total_pages = max(1, -(-total // page_size))

        listings = [_parse_next_posting(p) for p in listings_raw]
        return [l for l in listings if l], total_pages
    except Exception:
        return [], current_page


def _parse_next_posting(p: dict) -> Optional[Dict[str, Any]]:
    try:
        price_obj = p.get("price") or {}
        raw_price = price_obj.get("amount") or price_obj.get("value") or p.get("precio")
        currency_raw = (price_obj.get("currency") or price_obj.get("moneda") or "USD").upper()
        currency = "USD" if "USD" in currency_raw or "U$S" in currency_raw else "ARS"

        address = (
            p.get("address")
            or p.get("direccion")
            or p.get("location", {}).get("address")
            or ""
        )

        covered = _coerce_float(
            p.get("coveredSurface") or p.get("superficieCubierta") or p.get("superficie_cubierta")
        )
        total_m2 = _coerce_float(
            p.get("totalSurface") or p.get("superficieTotal") or p.get("superficie_total")
        )
        ambientes = _coerce_int(p.get("rooms") or p.get("ambientes"))
        dormitorios = _coerce_int(p.get("bedrooms") or p.get("dormitorios"))
        banos = _coerce_int(p.get("bathrooms") or p.get("banos"))

        prop_type = (
            p.get("propertyType", {}).get("name")
            or p.get("tipo")
            or p.get("type")
            or ""
        )

        re_name = (
            p.get("publisher", {}).get("name")
            or p.get("inmobiliaria")
            or ""
        )

        se_id = str(p.get("id") or p.get("propertyId") or "")
        url_path = p.get("url") or p.get("link") or ""
        if url_path and not url_path.startswith("http"):
            url_path = f"{BASE_URL}{url_path}"

        return {
            "search_engine_id": se_id,
            "type": prop_type,
            "ambientes": ambientes,
            "dormitorios": dormitorios,
            "banos": banos,
            "price": _coerce_float(raw_price),
            "currency": currency,
            "address": address,
            "covered_m2": covered,
            "total_m2": total_m2,
            "real_estate": re_name,
            "url": url_path,
        }
    except Exception:
        return None


# ── DOM fallback ────────────────────────────────────────────────────────────

async def _extract_from_dom(page, current_page: int):
    selectors = [".card", ".listing__item", "[class*='card']", "article"]
    cards = []
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=5000)
            cards = await page.query_selector_all(sel)
            if cards:
                break
        except Exception:
            continue

    listings = []
    for card in cards:
        try:
            listing = await _parse_dom_card(card)
            if listing and listing.get("url"):
                listings.append(listing)
        except Exception:
            pass

    total_pages = current_page
    try:
        paging = await page.query_selector(".pagination, [class*='pagination']")
        if paging:
            pages_text = await paging.inner_text()
            nums = re.findall(r"\d+", pages_text)
            if nums:
                total_pages = max(int(n) for n in nums)
    except Exception:
        pass

    return listings, total_pages


async def _parse_dom_card(card) -> Optional[Dict[str, Any]]:
    async def text(sel):
        el = await card.query_selector(sel)
        return (await el.inner_text()).strip() if el else ""

    price_text = await text(".card__price, .price, [class*='price']")
    address_text = await text(".card__address, .address, [class*='address'], [class*='location']")
    features_text = await text(".card__features, .features, [class*='features']")

    link_el = await card.query_selector("a[href]")
    href = await link_el.get_attribute("href") if link_el else ""
    if href and not href.startswith("http"):
        href = f"{BASE_URL}{href}"

    se_id = ""
    m = re.search(r"-(\d{6,12})(?:\.html|$)", href or "")
    if m:
        se_id = m.group(1)

    price, currency = _parse_price_text(price_text)
    covered, total, ambientes = _parse_features_text(features_text)

    if not href:
        return None
    return {
        "search_engine_id": se_id,
        "type": "",
        "ambientes": ambientes,
        "price": price,
        "currency": currency,
        "address": address_text,
        "covered_m2": covered,
        "total_m2": total,
        "url": href,
    }


# ── Detail page ─────────────────────────────────────────────────────────────

async def _scrape_detail(page, url: str) -> Dict[str, Any]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.5)

        next_data = await page.evaluate(
            "() => { try { return JSON.parse(document.getElementById('__NEXT_DATA__').textContent); } catch(e) { return null; } }"
        )

        if next_data:
            return _extract_detail_from_next_data(next_data)
        return await _extract_detail_from_dom(page)
    except Exception as e:
        print(f"[AP-DETAIL-ERR] {url[-80:]} → {type(e).__name__}: {e}")
        return {}


def _extract_detail_from_next_data(data: dict) -> Dict[str, Any]:
    try:
        props = data.get("props", {}).get("pageProps", {})
        posting = props.get("property") or props.get("posting") or props or {}

        amenities = []
        for a in posting.get("amenities") or posting.get("caracteristicas") or []:
            if isinstance(a, dict):
                amenities.append(a.get("name") or a.get("label") or "")
            elif isinstance(a, str):
                amenities.append(a)

        expenses_raw = posting.get("expenses") or posting.get("expensas") or {}
        expenses = _coerce_float(expenses_raw.get("amount") if isinstance(expenses_raw, dict) else expenses_raw)
        expenses_currency = (expenses_raw.get("currency") if isinstance(expenses_raw, dict) else "ARS") or "ARS"

        publisher = posting.get("publisher") or posting.get("inmobiliaria") or {}
        phone = (publisher.get("phone") if isinstance(publisher, dict) else "") or ""
        re_name = (publisher.get("name") if isinstance(publisher, dict) else str(publisher)) or ""

        published_at = posting.get("createdAt") or posting.get("fechaPublicacion") or ""
        floor = str(posting.get("floor") or posting.get("piso") or "")
        orientation = posting.get("orientation") or posting.get("orientacion") or ""
        age = _coerce_int(str(posting.get("antiquity") or posting.get("antiguedad") or ""))
        condition = posting.get("condition") or posting.get("estado") or ""
        parking_raw = posting.get("garage") or posting.get("cochera")
        parking = bool(parking_raw) if parking_raw is not None else None
        toilettes = _coerce_int(
            posting.get("toilettes")
            or posting.get("halfBathrooms")
            or posting.get("toilettes_cantidad")
        )

        description = (
            posting.get("description")
            or posting.get("descripcion")
            or posting.get("observations")
            or ""
        ).strip()

        images_raw = (
            posting.get("photos")
            or posting.get("images")
            or posting.get("gallery")
            or posting.get("fotos")
            or []
        )
        images = []
        for img in images_raw:
            if isinstance(img, str) and img.startswith("http"):
                images.append(img)
            elif isinstance(img, dict):
                url = img.get("url") or img.get("src") or img.get("image") or ""
                if url.startswith("http"):
                    images.append(url)

        return {
            "amenities": [a for a in amenities if a],
            "expenses": expenses,
            "expenses_currency": expenses_currency,
            "real_estate": re_name or None,
            "real_estate_phone": phone or None,
            "published_at": published_at or None,
            "floor": floor or None,
            "orientation": orientation or None,
            "age_years": age,
            "condition": condition or None,
            "parking": parking,
            "toilettes": toilettes,
            "description": description or None,
            "images": images or None,
        }
    except Exception:
        return {}


async def _extract_detail_from_dom(page) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    try:
        amenities = []
        for sel in ["[class*='amenity']", "[class*='caracteristica']", ".feature-item"]:
            els = await page.query_selector_all(sel)
            for el in els:
                t = (await el.inner_text()).strip()
                if t:
                    amenities.append(t)
            if amenities:
                break
        result["amenities"] = amenities

        for sel in ["[class*='expense']", "[class*='expensa']"]:
            el = await page.query_selector(sel)
            if el:
                exp_text = (await el.inner_text()).strip()
                val, curr = _parse_price_text(exp_text)
                result["expenses"] = val
                result["expenses_currency"] = curr
                break

        for sel in ["[class*='publisher'] [class*='name']", "[class*='inmobiliaria']"]:
            el = await page.query_selector(sel)
            if el:
                result["real_estate"] = (await el.inner_text()).strip()
                break

        for sel in ["[class*='description']", "[class*='descripcion']", "[id*='description']"]:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if len(text) > 20:
                    result["description"] = text[:2000]
                    break

        imgs = []
        for sel in ["[class*='gallery'] img", "[class*='carousel'] img", "[class*='slider'] img", "img[src*='cdn']"]:
            els = await page.query_selector_all(sel)
            for img_el in els:
                src = await img_el.get_attribute("src") or await img_el.get_attribute("data-src") or ""
                if src.startswith("http") and not src.endswith(".svg"):
                    imgs.append(src)
            if imgs:
                break
        if imgs:
            result["images"] = imgs[:40]
    except Exception:
        pass
    return result


# ── Helpers ─────────────────────────────────────────────────────────────────

def _parse_price_text(text: str) -> Tuple[Optional[float], str]:
    if not text:
        return None, "USD"
    upper = text.upper()
    currency = "USD" if ("U$S" in upper or "USD" in upper or "US$" in upper) else "ARS" if "$" in text else "USD"
    nums = re.findall(r"\d+", text.replace(".", "").replace(",", ""))
    val = float(nums[0]) if nums else None
    return val, currency


def _parse_features_text(text: str) -> Tuple[Optional[float], Optional[float], Optional[int]]:
    covered = total = ambientes = None
    m = re.search(r"(\d+)\s*m[²2]\s*cub", text, re.IGNORECASE)
    if m:
        covered = float(m.group(1))
    m = re.search(r"(\d+)\s*m[²2]\s*tot", text, re.IGNORECASE)
    if m:
        total = float(m.group(1))
    if not total:
        # Bare m² fallback — use a negative lookahead so we don't re-match a
        # span that already has a "cub" or "tot" label (which would cause
        # covered_m2 and total_m2 to end up with the same value).
        m = re.search(r"(\d+)\s*m[²2](?!\s*(?:cub|tot))", text, re.IGNORECASE)
        if m:
            total = float(m.group(1))
    m = re.search(r"(\d+)\s*amb", text, re.IGNORECASE)
    if m:
        ambientes = int(m.group(1))
    return covered, total, ambientes


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(".", "").replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _coerce_int(v) -> Optional[int]:
    f = _coerce_float(v)
    return int(f) if f is not None else None
