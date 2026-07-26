import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Set

from .base import BaseScraper, UA, coerce_float, coerce_int

logger = logging.getLogger(__name__)

BASE_URL = "https://www.argenprop.com"


class ArgenpropScraper(BaseScraper):
    BASE_URL = BASE_URL

    def _page_url(self, search_filter: str, page: int) -> str:
        """Argenprop uses ?pagina-N (dash) query param."""
        base = f"{BASE_URL}{search_filter}"
        if page <= 1:
            return base
        sep = "&" if "?" in search_filter else "?"
        return f"{base}{sep}pagina-{page}"

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
            ).filter(s => s && s.startsWith('http') && !s.endsWith('.svg') && !s.includes('_a/'));

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
        await page.wait_for_selector('.pagination', timeout=5000)
    except Exception:
        return 1
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
    except Exception as e:
        logger.warning("AP pagination extraction failed: %s", e)
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

        # DOM values override ld+json (DOM is more detailed)
        dom_result = await _extract_detail_from_dom(page)
        for k, v in dom_result.items():
            if v is not None:
                if k == "images" and k in result and isinstance(v, list) and isinstance(result[k], list):
                    for img in v:
                        if img not in result[k]:
                            result[k].append(img)
                else:
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
    """Extract fields from the detail page DOM."""
    result: Dict[str, Any] = {}

    try:
        # ── Structured extraction via JS ─────────────────────────────────
        data = await page.evaluate(r"""() => {
            const r = {};

            // ── Images from gallery (CSS background-image in data-open-gallery divs)
            const imgs = [];
            document.querySelectorAll('[data-open-gallery] div[style]').forEach(div => {
                const style = div.getAttribute('style') || '';
                const m = style.match(/url\((https?:\/\/[^)]+)\)/);
                if (m) {
                    const src = m[1];
                    if (src.includes('static-content') && !src.includes('_a/') && !src.includes('photo_placeholder')) {
                        if (!imgs.includes(src)) imgs.push(src);
                    }
                }
            });
            // Fallback: check ld+json images if gallery extraction yields nothing
            if (imgs.length === 0) {
                document.querySelectorAll('img').forEach(i => {
                    const src = i.src || i.getAttribute('data-src') || '';
                    if (src.startsWith('http') && src.includes('static-content') && !src.includes('_a/') && !src.includes('similar')) {
                        if (!imgs.includes(src)) imgs.push(src);
                    }
                });
            }
            r.images = imgs.slice(0, 40);

            // ── Property specs from main features ─────────────────────────
            const mainFeatures = {};
            document.querySelectorAll('.property-main-features li[title]').forEach(li => {
                const title = li.getAttribute('title');
                const text = (li.querySelector('.strong') || li.querySelector('p')).textContent.trim();
                mainFeatures[title] = text;
            });

            // Helper: extract first number from text like "250 m² Cubierta"
            function num(text) {
                if (!text) return null;
                const m = text.match(/\d[\d.,]*/);
                if (!m) return null;
                return parseFloat(m[0].replace(',', '.'));
            }

            // Map property-main-features to fields
            if (mainFeatures['Sup. cubierta']) r.covered_m2 = num(mainFeatures['Sup. cubierta']);
            if (mainFeatures['Sup. terreno']) r.total_m2 = num(mainFeatures['Sup. terreno']);
            if (mainFeatures['Dormitorios']) r.dormitorios = num(mainFeatures['Dormitorios']);
            if (mainFeatures['Baños']) r.banos = num(mainFeatures['Baños']);
            if (mainFeatures['Ambientes']) r.ambientes = num(mainFeatures['Ambientes']);
            if (mainFeatures['Cocheras']) r.parking = num(mainFeatures['Cocheras']) > 0;
            if (mainFeatures['Toilettes']) r.toilettes = num(mainFeatures['Toilettes']);
            if (mainFeatures['Antigüedad']) r.age_years = num(mainFeatures['Antigüedad']);
            if (mainFeatures['Estado']) r.condition = mainFeatures['Estado'];
            if (mainFeatures['Orientación']) r.orientation = mainFeatures['Orientación'];

            // section-caracteristicas: extract Cant. Plantas for floor
            document.querySelectorAll('#section-caracteristicas li h3').forEach(h3 => {
                const text = h3.textContent.trim();
                const m = text.match(/Cant\.\s*Plantas:\s*(\d+)/);
                if (m) r.floor = m[1];
            });

            // ── Surface area fallbacks from section-superficie ───────────
            document.querySelectorAll('#section-superficie li h3').forEach(h3 => {
                const text = h3.textContent.trim();
                const val = num(text);
                if (val === null) return;
                if (text.includes('Sup. Cubierta') && !r.covered_m2) r.covered_m2 = val;
                if (text.includes('Sup. Terreno') && !r.total_m2) r.total_m2 = val;
                if (text.includes('Sup. Total')) r.total_m2 = val;
            });

            // ── Location from map data attributes ─────────────────────────
            const map = document.querySelector('.leaflet-container');
            if (map) {
                const lat = map.getAttribute('data-latitude');
                const lng = map.getAttribute('data-longitude');
                if (lat) r.lat = parseFloat(lat.replace(',', '.'));
                if (lng) r.lng = parseFloat(lng.replace(',', '.'));
            }

            // ── Amenities from ambientes/instalaciones/servicios sections ─
            const amenities = [];
            ['section-ambientes-casa', 'section-instalaciones-casa', 'section-servicios-casa'].forEach(id => {
                document.querySelectorAll('#' + id + ' li.property-features-item h3').forEach(h3 => {
                    const t = h3.textContent.trim();
                    if (t && !amenities.includes(t)) amenities.push(t);
                });
            });
            r.amenities = amenities;

            // ── Publisher and contact info ────────────────────────────────
            const pubEl = document.querySelector('#avisos-anunciante-sup, .form-details-heading a');
            if (pubEl) r.real_estate = pubEl.textContent.trim();

            // Phone
            const phoneEl = document.querySelector('.form-detail-phone-number');
            if (phoneEl) r.real_estate_phone = phoneEl.textContent.trim();

            return r;
        }""")

        if data:
            # Images
            if data.get("images"):
                result["images"] = data["images"]

            # Property fields
            for key in ["covered_m2", "total_m2", "dormitorios", "banos",
                        "ambientes", "parking", "toilettes", "age_years", "condition",
                        "orientation", "floor", "lat", "lng"]:
                if data.get(key) is not None:
                    result[key] = data[key]

            # Amenities
            if data.get("amenities"):
                result["amenities"] = data["amenities"]

            # Publisher
            if data.get("real_estate"):
                result["real_estate"] = data["real_estate"]
            if data.get("real_estate_phone"):
                result["real_estate_phone"] = data["real_estate_phone"]

        # ── Description (separate, uses innerText for line breaks) ────────
        desc_el = await page.query_selector('.section-description--content')
        if desc_el:
            desc_text = (await desc_el.inner_text()).strip()
            if desc_text:
                result["description"] = desc_text[:2000]

    except Exception as e:
        logger.error("AP DOM extraction error: %s: %s", type(e).__name__, e)

    return result
