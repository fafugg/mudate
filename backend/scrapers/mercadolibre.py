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
        MAX_PAGES = 500

        async with self.launch_browser() as search_page:
            context = search_page.context

            # Phase 1: Navigate to first page
            url = f"{BASE_URL}{search_filter}"
            if progress_callback:
                progress_callback(
                    f"Cargando página 1 — {len(results)} propiedades",
                    len(results),
                    len(results),
                )

            await search_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # Extract listings from all pages via click-based pagination
            current_page = 1
            while current_page <= MAX_PAGES:
                if cancel_check and cancel_check():
                    break

                # Extract listings from current page
                card_listings = self._extract_from_json_ld(await search_page.content())

                if not card_listings:
                    break

                # Process each listing using a separate tab for details
                for card in card_listings:
                    se_id = card.get("search_engine_id") or ""
                    if se_id in seen_ids:
                        continue
                    seen_ids.add(se_id)

                    is_known = bool(existing_ids and se_id and se_id in existing_ids)
                    if progress_callback:
                        action = "Verificando precio" if is_known else "Descargando detalle"
                        progress_callback(
                            f"Pág {current_page} — {action} {len(results) + 1}/{len(results) + len(card_listings)}",
                            len(results),
                            len(results) + len(card_listings),
                        )

                    if not is_known and card.get("url"):
                        # Open detail page in a new tab to preserve search page state
                        detail_page = await context.new_page()
                        try:
                            detail = await _scrape_detail(detail_page, card["url"])
                            card.update({k: v for k, v in detail.items() if v is not None})
                        finally:
                            await detail_page.close()

                    card["price_per_m2"] = self.compute_price_per_m2(
                        card.get("price"), card.get("covered_m2") or card.get("total_m2")
                    )
                    results.append(card)

                    if not is_known:
                        await asyncio.sleep(0.5)

                # Try to click "Siguiente" (Next) button
                next_clicked = await self._click_next_page(search_page)
                if not next_clicked:
                    break

                current_page += 1
                await asyncio.sleep(self.delay)

        return results

    async def _click_next_page(self, page) -> bool:
        """Click the 'Siguiente' (Next) pagination button. Returns True if successful."""
        try:
            # Dismiss any overlays (coach marks, cookie consent)
            await page.evaluate("""() => {
                // Remove coach marks overlay
                document.querySelectorAll('.andes-coach-marks__overlay, .andes-coach-marks').forEach(el => el.remove());
                // Click cookie consent buttons if present
                const cookieBtns = document.querySelectorAll('[class*="cookie-consent"] button, [class*="cookie"] button');
                cookieBtns.forEach(btn => {
                    if (btn.textContent.includes('Aceptar') || btn.textContent.includes('OK') || btn.textContent.includes('Entendido')) {
                        btn.click();
                    }
                });
            }""")
            await asyncio.sleep(0.5)

            # Get current page number
            current_page = await page.evaluate("""() => {
                const el = document.querySelector('.andes-pagination__button--current');
                return el ? parseInt(el.textContent.trim()) : 0;
            }""")

            # Find the next page button by aria-label
            next_btn = await page.query_selector(f'a[aria-label="Ir a la página {current_page + 1}"]')

            if not next_btn:
                # Fallback: find any non-disabled, non-current pagination button
                next_btn = await page.query_selector(
                    'li.andes-pagination__button:not(.andes-pagination__button--disabled):not(.andes-pagination__button--current) a'
                )

            if not next_btn:
                return False

            # Click the next button and wait for navigation
            # Use expect_navigation to handle the page change properly
            try:
                async with page.expect_navigation(timeout=10000):
                    await page.evaluate("(el) => { el.click(); }", next_btn)
            except Exception:
                # Navigation might not always trigger a full page load
                pass

            # Wait for the new page to load
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            await asyncio.sleep(2)

            # Verify we're on the next page
            new_page = await page.evaluate("""() => {
                const el = document.querySelector('.andes-pagination__button--current');
                return el ? parseInt(el.textContent.trim()) : 0;
            }""")

            return new_page > current_page

        except Exception as e:
            logger.error("ML pagination error: %s: %s", type(e).__name__, e)
            return False

    def _extract_from_json_ld(self, html: str) -> List[Dict[str, Any]]:
        """Extract listings from the JSON-LD script tag and polycard JSON."""
        try:
            # Extract from JSON-LD
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

            # Extract addresses from polycard JSON
            polycard_addresses = _extract_polycard_addresses(html)

            # Merge polycard addresses into listings (match by position)
            for i, listing in enumerate(listings):
                if i < len(polycard_addresses) and polycard_addresses[i]:
                    listing["address"] = polycard_addresses[i]

            return listings
        except Exception as e:
            logger.error("ML JSON-LD extraction error: %s: %s", type(e).__name__, e)
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

        # Extract property type from URL domain
        # e.g. casa.mercadolibre.com.ar → Casa, departamento.mercadolibre.com.ar → Departamento
        prop_type = None
        url_lower = url.lower()
        type_map = {
            "casa.": "Casa",
            "departamento.": "Departamento",
            "ph.": "PH",
            "terreno.": "Terreno",
            "local.": "Local Comercial",
            "oficina.": "Oficina",
        }
        for domain_prefix, type_name in type_map.items():
            if domain_prefix in url_lower:
                prop_type = type_name
                break

        # Also try from listing name
        name = item.get("name", "")
        if not prop_type and name:
            name_lower = name.lower()
            if "casa" in name_lower or "chalet" in name_lower:
                prop_type = "Casa"
            elif "departamento" in name_lower or "depto" in name_lower or "dúplex" in name_lower or "duplex" in name_lower:
                prop_type = "Departamento"
            elif "ph" in name_lower.split():
                prop_type = "PH"
            elif "local" in name_lower:
                prop_type = "Local Comercial"
            elif "oficina" in name_lower:
                prop_type = "Oficina"

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

        # Extract dormitorios from name (e.g. "3 dormitorios")
        dormitorios = None
        m = re.search(r"(\d+)\s*dorm", name, re.IGNORECASE)
        if m:
            dormitorios = int(m.group(1))

        return {
            "search_engine_id": se_id,
            "type": prop_type,
            "ambientes": coerce_int(item.get("numberOfRooms")),
            "dormitorios": dormitorios,
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
    except Exception as e:
        logger.error("ML parse error: %s: %s", type(e).__name__, e)
        return None


async def _scrape_detail(page, url: str) -> Dict[str, Any]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait for gallery images to load
        try:
            await page.wait_for_function(
                """() => {
                    const imgs = document.querySelectorAll('img[src*="mlstatic"], img[src*="cdn"]');
                    return imgs.length >= 2;
                }""",
                timeout=8000,
            )
        except Exception:
            await asyncio.sleep(3)

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

        # Extract description from #description element
        desc_el = await page.query_selector('#description')
        if desc_el:
            desc_text = (await desc_el.inner_text()).strip()
            # Clean up the description - remove "Descripción" header if present
            if desc_text.startswith("Descripción"):
                desc_text = desc_text[len("Descripción"):].strip()
            if desc_text and len(desc_text) > 20:
                result["description"] = desc_text[:2000]

        # Extract technical specifications from tables
        # Tables use <th> for labels and <td> for values
        specs_data = await page.evaluate("""() => {
            const tables = document.querySelectorAll('table');
            const allSpecs = {};
            const amenities = [];

            for (const table of tables) {
                const rows = table.querySelectorAll('tr');
                for (const row of rows) {
                    const th = row.querySelector('th');
                    const td = row.querySelector('td');
                    if (th && td) {
                        const label = th.textContent.trim();
                        const value = td.textContent.trim();
                        allSpecs[label] = value;

                        // Collect amenities (value is "Sí")
                        if (value === 'Sí') {
                            amenities.push(label);
                        }
                    }
                }
            }
            return { specs: allSpecs, amenities };
        }""")

        specs = specs_data.get("specs", {})
        amenities_from_tables = specs_data.get("amenities", [])

        # Map technical specs to house fields
        # Extract numbers from values that may contain units (e.g., "401 m²")
        def extract_number(text: str) -> Optional[float]:
            m = re.search(r"([\d.,]+)", text)
            if m:
                return coerce_float(m.group(1).replace(",", "."))
            return None

        if "Superficie total" in specs:
            result["total_m2"] = extract_number(specs["Superficie total"])
        if "Superficie cubierta" in specs:
            result["covered_m2"] = extract_number(specs["Superficie cubierta"])
        if "Dormitorios" in specs:
            result["dormitorios"] = coerce_int(specs["Dormitorios"])
        if "Baños" in specs:
            result["banos"] = coerce_int(specs["Baños"])
        if "Cocheras" in specs:
            result["parking"] = coerce_int(specs["Cocheras"]) > 0
        if "Antigüedad" in specs:
            # Extract number from "43 años"
            m = re.search(r"(\d+)", specs["Antigüedad"])
            if m:
                result["age_years"] = int(m.group(1))
        if "Ambientes" in specs:
            result["ambientes"] = coerce_int(specs["Ambientes"])
        if "Cantidad de pisos" in specs:
            result["floor"] = specs["Cantidad de pisos"]

        # Use amenities from tables (only those with "Sí")
        if amenities_from_tables:
            result["amenities"] = amenities_from_tables

        # Extract expenses from DOM
        expenses_text = await _safe_text(page, "[class*='expense'], [class*='expensa'], [class*='expensas']")
        if expenses_text:
            val, curr = parse_price(expenses_text)
            if val:
                result["expenses"] = val
                result["expenses_currency"] = curr

        # Extract address from DOM - only if we don't already have a good address from search page
        # The search page polycard JSON provides better addresses than the detail page DOM
        if not result.get("address") or len(result.get("address", "")) < 20:
            addr_text = await _safe_text(page, "[class*='location'] address, [class*='address'] address")
            if addr_text and len(addr_text) < 200 and "whatsapp" not in addr_text.lower():
                result["address"] = addr_text

        # Extract house images from gallery (only gallery-image__image class)
        # This filters out MercadoLibre banners, real estate logos, and map images
        imgs = await page.evaluate("""() => {
            return [...document.querySelectorAll('img.gallery-image__image')]
                .map(i => i.src)
                .filter(s => s && s.startsWith('http'))
                .slice(0, 40);
        }""")
        if imgs:
            existing = result.get("images", [])
            for img in imgs:
                if img not in existing:
                    existing.append(img)
            result["images"] = existing

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

        # Images - filter out -O.webp (preview) versions, keep only -F-null.webp (full)
        imgs = []
        img_data = data.get("image", [])
        if isinstance(img_data, str):
            img_data = [img_data]
        if isinstance(img_data, list):
            for img in img_data:
                url = ""
                if isinstance(img, str) and img.startswith("http"):
                    url = img
                elif isinstance(img, dict):
                    url = img.get("url", "")
                    if url and not url.startswith("http"):
                        url = ""
                # Skip -O.webp images (preview versions)
                if url and "-O.webp" not in url:
                    imgs.append(url)
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


def _extract_polycard_addresses(html: str) -> List[str]:
    """Extract addresses from the polycard JSON embedded in the HTML.

    Each listing has a location.text field with the full street address.
    Returns a list of addresses in the same order as the listings.
    """
    addresses = []
    try:
        # Find all location.text values in the polycard JSON
        pattern = r'"location"\s*:\s*\{[^}]*"text"\s*:\s*"([^"]+)"'
        matches = re.findall(pattern, html)
        addresses = [m for m in matches if m]
    except Exception as e:
        logger.error("ML polycard address extraction error: %s: %s", type(e).__name__, e)
    return addresses
