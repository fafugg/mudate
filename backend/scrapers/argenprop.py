import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .base import BaseScraper, UA, coerce_float, coerce_int, parse_price

logger = logging.getLogger(__name__)

BASE_URL = "https://www.argenprop.com"


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
        existing_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        all_raw_cards: List[dict] = []

        async with self.launch_browser() as page:
            # ── Phase 1: Collect all card data from all pages ──────────────
            current_page = 1
            total_pages = 1

            while current_page <= min(total_pages, 500):
                if cancel_check and cancel_check():
                    break

                url = self._page_url(search_filter, current_page)
                if progress_callback:
                    progress_callback(
                        f"Cargando página {current_page}/{total_pages} — {len(all_raw_cards)} propiedades",
                        len(all_raw_cards),
                        len(all_raw_cards),
                    )

                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)

                if resp and resp.status == 403:
                    logger.warning("Argenprop returned 403 on %s", url)
                    break

                # Accept cookies if present
                try:
                    btn = await page.query_selector(
                        'button:has-text("Acepto"), button:has-text("Aceptar")'
                    )
                    if btn:
                        await btn.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

                # Extract cards via JavaScript
                cards = await _extract_cards_js(page)
                if not cards:
                    break

                added = 0
                for c in cards:
                    cid = c.get("id", "")
                    if cid and cid in {x.get("id") for x in all_raw_cards}:
                        continue
                    all_raw_cards.append(c)
                    added += 1

                if added == 0:
                    break

                # Extract total pages from pagination
                total_pages = await _extract_total_pages(page)

                current_page += 1
                await asyncio.sleep(self.delay)

            # ── Phase 2: Visit detail pages for new listings ───────────────
            total = len(all_raw_cards)
            sem = asyncio.Semaphore(2)

            async def _process_card(raw: dict) -> Dict[str, Any]:
                listing = _parse_card(raw)
                se_id = listing.get("search_engine_id") or ""
                is_known = bool(existing_ids and se_id and se_id in existing_ids)

                if not is_known and listing.get("url"):
                    async with sem:
                        if cancel_check and cancel_check():
                            return {}
                        detail_page = await context.new_page()
                        try:
                            detail = await _scrape_detail(detail_page, listing["url"])
                            listing.update({k: v for k, v in detail.items() if v is not None})
                            await asyncio.sleep(0.5)
                        finally:
                            await detail_page.close()

                listing["price_per_m2"] = self.compute_price_per_m2(
                    listing.get("price"), listing.get("covered_m2") or listing.get("total_m2")
                )

                if progress_callback:
                    action = "Verificando" if is_known else "Descargando detalle"
                    completed = results_lock.get("count", 0) + 1
                    results_lock["count"] = completed
                    progress_callback(
                        f"{action} {completed}/{total}", completed, total
                    )
                return listing

            context = page.context
            results_lock = {"count": 0}
            raw_results = await asyncio.gather(
                *[_process_card(raw) for raw in all_raw_cards]
            )
            results = [r for r in raw_results if r]

        return results


# ── Card extraction (JS, one call per page) ──────────────────────────────────

async def _extract_cards_js(page) -> list:
    """Extract card data from Argenprop .card elements using their data attributes."""
    try:
        await page.wait_for_selector(".card", timeout=10000)
    except Exception:
        return []

    return await page.evaluate("""() => {
        return [...document.querySelectorAll('.card')].map(card => {
            const href = card.href || '';
            const id = card.getAttribute('data-item-card') || card.getAttribute('idaviso') || '';
            const dormitorios = card.getAttribute('dormitorios') || '';
            const ambientes = card.getAttribute('ambientes') || '';
            const monto = card.getAttribute('montooperacion') || '';
            const idMoneda = card.getAttribute('idmoneda') || '';
            const idTipoProp = card.getAttribute('idtipopropiedad') || '';

            // Text elements
            const currencyEl = card.querySelector('.card__currency');
            const addressEl = card.querySelector('.card__address');
            const titlePrimary = card.querySelector('.card__title--primary');
            const titleH2 = card.querySelector('.card__title');

            // Features from spans
            const featureSpans = [...card.querySelectorAll('span')].map(s => s.textContent.trim()).filter(t =>
                /m[²2]|dorm|amb|baño|coch|año/i.test(t)
            );

            // Images (data-src for lazy loaded, src for first)
            const images = [...card.querySelectorAll('img')].map(i =>
                i.getAttribute('data-src') || i.src || ''
            ).filter(s => s && s.startsWith('http') && !s.endsWith('.svg'));

            return {
                id,
                href,
                monto,
                idMoneda,
                idTipoProp,
                dormitorios,
                ambientes,
                currencyText: currencyEl ? currencyEl.textContent.trim() : '',
                address: addressEl ? addressEl.textContent.trim() : '',
                titlePrimary: titlePrimary ? titlePrimary.textContent.trim() : '',
                title: titleH2 ? titleH2.textContent.trim() : '',
                featureSpans,
                images,
            };
        });
    }""")


async def _extract_total_pages(page) -> int:
    """Extract total pages from pagination element."""
    try:
        total = await page.evaluate(r"""() => {
            const spans = document.querySelectorAll('.pagination__page span[data-link-href]');
            let maxPage = 1;
            for (const span of spans) {
                const href = span.getAttribute('data-link-href') || '';
                const m = href.match(/pagina-(\d+)/);
                if (m) {
                    const n = parseInt(m[1], 10);
                    if (n > maxPage) maxPage = n;
                }
            }
            return maxPage;
        }""")
        return total
    except Exception:
        return 1


# ── Card parsing ──────────────────────────────────────────────────────────────

def _parse_card(raw: dict) -> Dict[str, Any]:
    """Parse a raw card dict (from _extract_cards_js) into a listing dict."""
    url = raw.get("href", "")
    se_id = raw.get("id", "")

    # Price from data attribute
    monto = raw.get("monto", "")
    price = coerce_float(monto) if monto else None

    # Currency
    id_moneda = raw.get("idMoneda", "")
    currency = "USD" if id_moneda == "2" else "ARS"

    # Rooms from data attributes
    dormitorios = coerce_int(raw.get("dormitorios")) if raw.get("dormitorios") else None
    ambientes = coerce_int(raw.get("ambientes")) if raw.get("ambientes") else None

    # Extract m² and ambientes from feature spans
    covered_m2 = None
    total_m2 = None
    for span in raw.get("featureSpans", []):
        s = span.lower()
        m = re.search(r"(\d+)\s*m[²2]", s)
        if m:
            val = float(m.group(1))
            if "cub" in s:
                covered_m2 = val
            elif "tot" in s:
                total_m2 = val
            elif total_m2 is None and covered_m2 is None:
                total_m2 = val

    # Property type from ID
    tipo_map = {
        "1": "Departamento",
        "2": "Departamento",
        "3": "Casa",
        "4": "PH",
        "5": "Terreno",
        "6": "Local Comercial",
        "7": "Oficina",
    }
    prop_type = tipo_map.get(raw.get("idTipoProp", ""), "")

    address = raw.get("address", "")

    if not url:
        return {}

    return {
        "search_engine_id": se_id or None,
        "type": prop_type or None,
        "ambientes": ambientes,
        "dormitorios": dormitorios,
        "price": price,
        "currency": currency,
        "address": address or None,
        "covered_m2": covered_m2,
        "total_m2": total_m2,
        "url": url,
        "images": raw.get("images", [])[:5] or None,
    }


# ── Detail page ─────────────────────────────────────────────────────────────

async def _scrape_detail(page, url: str) -> Dict[str, Any]:
    """Navigate to a detail page, extract structured data from ld+json and DOM."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # Extract ld+json House/Apartment data
        ld_data = await page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (const s of scripts) {
                try {
                    const d = JSON.parse(s.textContent);
                    if (d['@type'] === 'House' || d['@type'] === 'Apartment' ||
                        d['@type'] === 'RealEstateListing' || d['@type'] === 'Residence') {
                        return d;
                    }
                } catch(e) {}
            }
            return null;
        }""")

        result: Dict[str, Any] = {}

        if ld_data:
            result.update(_parse_ld_data(ld_data))

        # DOM fallbacks for fields not in ld+json
        dom_result = await _extract_detail_from_dom(page)
        for k, v in dom_result.items():
            if v is not None:
                # For images, merge with existing (ld+json may have 1, DOM may have more)
                if k == "images" and k in result and isinstance(v, list) and isinstance(result[k], list):
                    for img in v:
                        if img not in result[k]:
                            result[k].append(img)
                elif k not in result:
                    result[k] = v

        return result
    except Exception as e:
        logger.error("AP detail error %s: %s: %s", url[-80:], type(e).__name__, e)
        return {}


def _parse_ld_data(data: dict) -> Dict[str, Any]:
    """Extract fields from ld+json House/Apartment data."""
    result: Dict[str, Any] = {}

    # Images
    img = data.get("image")
    if isinstance(img, str) and img.startswith("http"):
        result["images"] = [img]
    elif isinstance(img, list):
        result["images"] = [i for i in img if isinstance(i, str) and i.startswith("http")][:40]

    # Address
    addr = data.get("address")
    if isinstance(addr, dict):
        street = (addr.get("streetAddress") or "").strip()
        locality = (addr.get("addressLocality") or "").strip()
        region = (addr.get("addressRegion") or "").strip()
        parts = [p for p in [street, locality] if p]
        result["address"] = ", ".join(parts) if parts else None

    # Rooms
    if "numberOfRooms" in data:
        result["ambientes"] = coerce_int(data["numberOfRooms"])
    if "numberOfBedrooms" in data:
        result["dormitorios"] = coerce_int(data["numberOfBedrooms"])
    if "numberOfBathroomsTotal" in data:
        result["banos"] = coerce_int(data["numberOfBathroomsTotal"])

    # Description
    desc = (data.get("description") or "").strip()
    if desc:
        result["description"] = desc[:2000]

    # Property type
    type_map = {"House": "Casa", "Apartment": "Departamento", "Residence": "Casa"}
    ld_type = data.get("@type", "")
    if ld_type in type_map:
        result["type"] = type_map[ld_type]

    return result


async def _extract_detail_from_dom(page) -> Dict[str, Any]:
    """Extract additional fields from the detail page DOM."""
    result: Dict[str, Any] = {}

    try:
        # Images from gallery
        imgs = await page.evaluate("""() => {
            const imgs = [];
            document.querySelectorAll('img').forEach(i => {
                const src = i.src || i.getAttribute('data-src') || '';
                if (src.startsWith('http') && !src.endsWith('.svg') && src.includes('static-content')) {
                    if (!imgs.includes(src)) imgs.push(src);
                }
            });
            return imgs.slice(0, 40);
        }""")
        if imgs:
            result["images"] = imgs

        # Features/amenities from page text
        features = await page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('li, [class*="feature-item"], [class*="amenity"]').forEach(el => {
                const t = el.textContent.trim();
                if (t.length > 2 && t.length < 80 && !items.includes(t)) {
                    items.push(t);
                }
            });
            return items.slice(0, 30);
        }""")
        # Filter: only keep items that look like actual amenities/property features
        amenity_keywords = [
            "m²", "dorm", "amb", "baño", "coch", "año", "pisc", "pileta",
            "jardín", "jardin", "parrilla", "gimnasio", "seguridad", "ascensor",
            "aire", "calefacción", "calefaccion", "amueblado", "balcón", "balcon",
            "terraza", "patio", "lavadero", "quincho", "playroom", "sum",
            "alarma", "cámara", "camara", "gated", "cerrado", "service",
        ]
        # Exclude items that are clearly navigation/breadcrumbs or questions
        nav_patterns = [
            "emprendimientos", "countries", "barrios cerrados", "garantías",
            "noticias", "publicar", "argenprop", "venta", "alquiler",
            "¿cuántos", "¿cuántas", "ver más", "ver todas",
        ]
        result["amenities"] = [
            f for f in features
            if any(kw in f.lower() for kw in amenity_keywords)
            and not any(nav in f.lower() for nav in nav_patterns)
            and "?" not in f
        ]

        # Expenses
        exp_el = await page.query_selector("[class*='expense'], [class*='expensa']")
        if exp_el:
            exp_text = (await exp_el.inner_text()).strip()
            val, curr = parse_price(exp_text)
            if val:
                result["expenses"] = val
                result["expenses_currency"] = curr

        # Publisher/real estate
        pub_el = await page.query_selector(
            "[class*='publisher'] [class*='name'], [class*='inmobiliaria']"
        )
        if pub_el:
            result["real_estate"] = (await pub_el.inner_text()).strip()

        # Phone
        phone_el = await page.query_selector("[href*='tel:']")
        if phone_el:
            href = await phone_el.get_attribute("href") or ""
            result["real_estate_phone"] = href.replace("tel:", "").strip() or None

    except Exception as e:
        logger.error("AP DOM extraction error: %s: %s", type(e).__name__, e)

    return result


# ── Helpers ─────────────────────────────────────────────────────────────────

def _parse_price_text(text: str) -> Tuple[Optional[float], str]:
    return parse_price(text)
