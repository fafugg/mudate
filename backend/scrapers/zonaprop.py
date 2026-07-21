import asyncio
import datetime
import json
import logging
import os
import random
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .base import BaseScraper, UA, normalize_phone, parse_price

logger = logging.getLogger(__name__)

BASE_URL = "https://www.zonaprop.com.ar"

# Run headed locally (bypasses Cloudflare more reliably) but headless in Docker.
# /.dockerenv is created by Docker on every container — reliable, macOS-safe.
# PLAYWRIGHT_HEADLESS env var always overrides (1/true = force headless, 0/false = force headed).
_explicit = os.environ.get("PLAYWRIGHT_HEADLESS", "").lower()
_HEADLESS = (
    _explicit in ("1", "true", "yes")
    or (os.path.exists("/.dockerenv") and _explicit not in ("0", "false", "no"))
)

# Use separate browser profiles for headed (local) and headless (Docker) runs.
# This prevents a Docker/headless run from poisoning the local profile with
# bot-flagged Cloudflare cookies, which would block the headed browser too.
_PROFILE_DIR = os.path.expanduser(
    "~/.mudate_browser_headless" if _HEADLESS else "~/.mudate_browser"
)

_TYPE_MAP = {
    "House": "Casa",
    "SingleFamilyResidence": "Casa",
    "Apartment": "Departamento",
    "ApartmentComplex": "Departamento",
    "OfficeBuilding": "Oficina",
    "Store": "Local Comercial",
    "LodgingBusiness": "Local Comercial",
}

_ORI_ABBR = {
    "N": "Norte", "S": "Sur", "E": "Este", "O": "Oeste",
    "NE": "Noreste", "NO": "Noroeste", "SE": "Sureste", "SO": "Suroeste",
}
_ORI_WORDS = {"norte", "sur", "este", "oeste", "noreste", "noroeste", "sureste", "suroeste"}


class ZonapropScraper(BaseScraper):
    BASE_URL = BASE_URL

    async def scrape_search(
        self,
        search_filter: str,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        existing_ids: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        os.makedirs(_PROFILE_DIR, exist_ok=True)

        async with async_playwright() as p:
            # Headed mode bypasses Cloudflare more reliably locally.
            # In Docker (no display server) we must use headless instead.
            context = await p.chromium.launch_persistent_context(
                user_data_dir=_PROFILE_DIR,
                headless=_HEADLESS,
                user_agent=UA,
                viewport={"width": 1280, "height": 900},
                locale="es-AR",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    # Docker-only flags — never pass these locally (Cloudflare fingerprints them)
                    *([
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--log-level=3",
                    ] if _HEADLESS else [
                        # Start minimised so the browser never steals focus from
                        # whatever the user is working on.  The CDP connection and
                        # Cloudflare fingerprint are unaffected by window state.
                        "--start-minimized",
                    ]),
                ],
            )
            page = await context.new_page()

            # Block images, media and fonts in headless/Docker mode only.
            # In headed local mode we never intercept requests — Cloudflare's
            # fingerprinting detects route interception and treats it as a bot.
            if _HEADLESS:
                _BLOCK = {"image", "media", "font"}
                await page.route(
                    "**/*",
                    lambda route, req: (
                        route.abort() if req.resource_type in _BLOCK else route.continue_()
                    ),
                )

            try:
                # ── Phase 1: collect raw card data clicking through all pages ──
                all_raw_cards = await _collect_all_pages(
                    page, search_filter, progress_callback, cancel_check
                )

                # ── Phase 2: visit detail pages — 2 tabs in parallel ──────────
                total = len(all_raw_cards)
                completed = 0
                sem = asyncio.Semaphore(2)

                async def _process_card(raw: dict) -> Dict[str, Any]:
                    nonlocal completed
                    if cancel_check and cancel_check():
                        return {}

                    listing = _parse_card(raw)
                    se_id    = listing.get("search_engine_id") or ""
                    is_known = bool(existing_ids and se_id and se_id in existing_ids)

                    if not is_known and listing.get("url"):
                        async with sem:
                            if cancel_check and cancel_check():
                                return {}
                            detail_page = await context.new_page()
                            if _HEADLESS:
                                _BLOCK = {"image", "media", "font"}
                                await detail_page.route(
                                    "**/*",
                                    lambda route, req: (
                                        route.abort()
                                        if req.resource_type in _BLOCK
                                        else route.continue_()
                                    ),
                                )
                            try:
                                detail = await _scrape_detail(detail_page, listing["url"])
                                listing.update({k: v for k, v in detail.items() if v is not None})
                                await asyncio.sleep(random.uniform(0.3, 0.7))
                            finally:
                                await detail_page.close()

                    listing["price_per_m2"] = self.compute_price_per_m2(
                        listing.get("price"),
                        listing.get("covered_m2") or listing.get("total_m2"),
                    )
                    completed += 1
                    if progress_callback:
                        action = "Verificando" if is_known else "Descargando detalle"
                        progress_callback(
                            f"{action} {completed}/{total}", completed, total
                        )
                    return listing

                raw_results = await asyncio.gather(
                    *[_process_card(raw) for raw in all_raw_cards]
                )
                results = [r for r in raw_results if r]

            finally:
                await context.close()

        return results


# ── Phase 1: paginate via click ───────────────────────────────────────────────

async def _collect_all_pages(
    page,
    search_filter: str,
    progress_callback,
    cancel_check=None,
) -> list:
    all_raw: list = []
    seen_ids: set = set()

    resp = await page.goto(
        f"{BASE_URL}{search_filter}", wait_until="domcontentloaded", timeout=30000
    )
    await asyncio.sleep(2)

    if resp and resp.status == 403:
        return []

    await _accept_cookies(page)
    await asyncio.sleep(1)

    current_page = 1
    while current_page <= 500:
        if cancel_check and cancel_check():
            break
        if progress_callback:
            progress_callback(
                f"Cargando página {current_page} — {len(all_raw)} propiedades",
                len(all_raw), len(all_raw),
            )

        cards = await _extract_cards_js(page)
        if not cards:
            break

        added = 0
        for c in cards:
            cid = c.get("id", "")
            if cid and cid in seen_ids:
                continue
            if cid:
                seen_ids.add(cid)
            all_raw.append(c)
            added += 1

        if added == 0:
            break

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.5)

        next_page = current_page + 1
        clicked = await page.evaluate(f"""() => {{
            const target = String({next_page});
            const link = [...document.querySelectorAll('a[href]')].find(a =>
                a.textContent.trim() === target &&
                /pagina/i.test(a.href)
            );
            if (link) {{ link.click(); return true; }}
            return false;
        }}""")

        if not clicked:
            break

        first_id_before = cards[0].get("id", "") if cards else ""
        url_changed = False
        for _ in range(30):
            await asyncio.sleep(0.5)
            if f"pagina-{next_page}" in page.url:
                url_changed = True
                break
        if not url_changed:
            break

        for _ in range(20):
            await asyncio.sleep(0.5)
            first_id_now = await page.evaluate("""() => {
                const el = document.querySelector('[data-posting-type]');
                return el ? (el.getAttribute('data-id') || '') : '';
            }""")
            if first_id_now and first_id_now != first_id_before:
                break

        await asyncio.sleep(random.uniform(0.5, 1.0))
        current_page = next_page

    return all_raw


# ── Cookie consent ────────────────────────────────────────────────────────────

async def _accept_cookies(page) -> None:
    try:
        btn = await page.query_selector(
            'button:has-text("Acepto"), button:has-text("Aceptar"), '
            '#onetrust-accept-btn-handler, [class*="cookie"] button'
        )
        if btn:
            await btn.click()
    except Exception:
        pass


# ── Card extraction (JS, one call per page) ───────────────────────────────────

async def _extract_cards_js(page) -> list:
    try:
        await page.wait_for_selector("[data-posting-type]", timeout=10000)
    except Exception:
        return []

    return await page.evaluate("""() => {
        function isSuggested(card) {
            let el = card.parentElement;
            while (el) {
                if (el.className && typeof el.className === 'string' &&
                    el.className.includes('thin-postings-container')) return true;
                el = el.parentElement;
            }
            return false;
        }
        return [...document.querySelectorAll('[data-posting-type]')]
            .filter(card => !isSuggested(card))
            .map(card => {
            const id       = card.getAttribute('data-id') || '';
            const urlPath  = (card.getAttribute('data-to-posting') || '').split('?')[0];

            const priceEl  = card.querySelector('[data-qa="POSTING_CARD_PRICE"]');
            const expEl    = card.querySelector('[data-qa="expensas"]');
            const featEl   = card.querySelector('[data-qa="POSTING_CARD_FEATURES"]');
            const streetEl = card.querySelector('[class*="location-address"]');
            const locEl    = card.querySelector('[data-qa="POSTING_CARD_LOCATION"]');

            let propType = '';
            let datePosted = '';
            const ld = card.querySelector('script[type="application/ld+json"]');
            if (ld) {
                try {
                    const j = JSON.parse(ld.textContent);
                    propType = j['@type'] || (j.name || '').split('·')[0].trim();
                    datePosted = j['datePosted'] || j['datePublished'] || '';
                } catch(e) {}
            }

            // Dedicated m² icon elements — most reliable source for surface areas
            const cubEl  = card.querySelector('[class*="icon-scubierta"]');
            const totEl  = card.querySelector('[class*="icon-stotal"]');
            const cubText = cubEl ? (cubEl.parentElement || cubEl).textContent.trim() : '';
            const totText = totEl ? (totEl.parentElement || totEl).textContent.trim() : '';

            return {
                id,
                urlPath,
                priceText : priceEl  ? priceEl.textContent.trim()  : '',
                expText   : expEl    ? expEl.textContent.trim()     : '',
                featSpans : featEl   ? [...featEl.querySelectorAll('span')].map(s => s.textContent.trim()) : [],
                street    : streetEl ? streetEl.textContent.trim()  : '',
                location  : locEl    ? locEl.textContent.trim()     : '',
                propType,
                datePosted,
                cubText,
                totText,
            };
        });
    }""")


# ── Card parsing ──────────────────────────────────────────────────────────────

def _parse_card(raw: dict) -> Dict[str, Any]:
    url_path = raw.get("urlPath", "")
    url = f"{BASE_URL}{url_path}" if url_path else ""

    price, currency = _parse_price(raw.get("priceText", ""))
    exp_val, exp_curr = _parse_price(raw.get("expText", ""))

    # Use dedicated icon elements when available — they are unambiguous.
    covered_m2 = _parse_num(raw["cubText"]) if raw.get("cubText") else None
    total_m2   = _parse_num(raw["totText"]) if raw.get("totText") else None

    ambientes = dormitorios = banos = parking = None
    for span in raw.get("featSpans", []):
        s = span.lower()
        if "m²" in s or "m2" in s:
            # Only fall back to span-based m² parsing when the icon approach
            # didn't already provide a value for that specific field.
            val = _parse_num(span)
            if "tot" in s and total_m2 is None:
                total_m2 = val
            elif "cub" in s and covered_m2 is None:
                covered_m2 = val
            elif total_m2 is None and covered_m2 is None:
                total_m2 = val  # bare m² with no label → assume total
        elif "amb" in s:
            ambientes = _parse_int(span)
        elif "dorm" in s:
            dormitorios = _parse_int(span)
        elif "baño" in s or "bano" in s:
            banos = _parse_int(span)
        elif "coch" in s:
            n = _parse_int(span)
            parking = bool(n and n > 0)

    street = raw.get("street", "")
    location = raw.get("location", "")
    address = _clean_address(", ".join(filter(None, [street, location])))

    raw_type = raw.get("propType", "")
    prop_type = _TYPE_MAP.get(raw_type, raw_type) or None
    published_at = raw.get("datePosted") or None

    return {
        "search_engine_id": raw.get("id") or None,
        "published_at": published_at,
        "type": prop_type,
        "ambientes": ambientes,
        "dormitorios": dormitorios,
        "banos": banos,
        "price": price,
        "currency": currency,
        "expenses": exp_val,
        "expenses_currency": exp_curr if exp_val else None,
        "address": address or None,
        "covered_m2": covered_m2,
        "total_m2": total_m2,
        "parking": parking,
        "url": url,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Detail page — BeautifulSoup per-field extractors
# ══════════════════════════════════════════════════════════════════════════════

_GALLERY_FUNC = """() => {
    const imgs = document.querySelectorAll('[class*="imageGrid-module__imgProperty"]');
    const loaded = [...imgs].filter(img => {
        const s = img.getAttribute('src') || '';
        return s.startsWith('http') && !s.endsWith('.svg');
    });
    return loaded.length >= 2;
}"""


async def _wait_selector(page, selector: str, timeout: int) -> None:
    """Wait for a selector, silently swallowing timeouts."""
    try:
        await page.wait_for_selector(selector, timeout=timeout)
    except Exception:
        pass


async def _wait_gallery(page, timeout: int) -> None:
    """Wait for ≥2 gallery thumbnails to have a real src; fall back to portal container."""
    try:
        await page.wait_for_function(_GALLERY_FUNC, timeout=timeout)
    except Exception:
        try:
            await page.wait_for_selector(
                "#new-gallery-portal, #multimedia-content", timeout=4000
            )
        except Exception:
            pass


async def _scrape_detail(page, url: str) -> Dict[str, Any]:
    """Navigate to a detail page, get rendered HTML, extract all fields."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Run all three waits concurrently — worst case is max(each), not sum(each).
        # Previously sequential (up to 8+8+8 = 24 s); now up to max(6,6,8) = 8 s.
        await asyncio.gather(
            _wait_selector(page, '[class*="icon-stotal"]', 6000),
            _wait_selector(page, '[class*="publisherData-module__publisher-name"]', 6000),
            _wait_gallery(page, 8000),
        )

        html = await page.content()
        return _extract_detail(html)
    except Exception as e:
        logger.error("ZP detail error %s: %s: %s", url[-80:], type(e).__name__, e)
        return {}


def _extract_detail(html: str) -> Dict[str, Any]:
    """
    Parse a rendered Zonaprop detail page.
    Each field delegates to its own extractor — easy to test and fix in isolation.
    """
    soup = BeautifulSoup(html, "lxml")
    ld   = _get_ld_json(soup)

    result: Dict[str, Any] = {}

    def _set(key: str, val: Any) -> None:
        if val is not None:
            result[key] = val

    # ── Property type ─────────────────────────────────────────────────────────
    _set("type",              _get_type(soup))

    # ── Surface areas (icon classes are unambiguous) ──────────────────────────
    _set("covered_m2",        _get_covered_m2(soup))
    _set("total_m2",          _get_total_m2(soup))

    # ── Room counts ───────────────────────────────────────────────────────────
    _set("ambientes",         _get_ambientes(soup, ld))
    _set("dormitorios",       _get_dormitorios(soup, ld))
    _set("banos",             _get_banos(soup, ld))
    _set("toilettes",         _get_toilettes(soup))

    # ── Property features ─────────────────────────────────────────────────────
    _set("floor",             _get_floor(soup))
    _set("parking",           _get_parking(soup))
    _set("orientation",       _get_orientation(soup))
    _set("age_years",         _get_age_years(soup))
    _set("condition",         _get_condition(soup))

    # ── Financials ────────────────────────────────────────────────────────────
    exp_val, exp_curr = _get_expenses(soup)
    _set("expenses",          exp_val)
    _set("expenses_currency", exp_curr if exp_val else None)

    # ── Location & content ────────────────────────────────────────────────────
    _set("address",           _get_address(soup, ld))
    _set("description",       _get_description(soup, ld))
    _set("amenities",         _get_amenities(soup))
    _set("images",            _get_images(soup, ld))

    # ── Publisher ─────────────────────────────────────────────────────────────
    _set("real_estate",       _get_publisher(soup, ld))
    _set("real_estate_phone", _get_publisher_phone(ld))
    _set("published_at",      _get_published_at(soup, ld))

    return result


# ── Base helpers ──────────────────────────────────────────────────────────────

def _get_ld_json(soup: BeautifulSoup) -> dict:
    """Return the first ld+json block that describes the property itself."""
    PROP_TYPES = {"House", "Apartment", "ApartmentComplex", "SingleFamilyResidence", "Residence"}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict) and data.get("@type") in PROP_TYPES:
                return data
        except Exception:
            pass
    return {}


def _icon_container(soup: BeautifulSoup, icon_class: str):
    """Find an element whose class matches icon_class, return its nearest li/span/div ancestor."""
    el = soup.find(class_=re.compile(icon_class, re.I))
    if not el:
        return None
    return el.find_parent(["li", "span", "div"]) or el.parent


def _icon_num(soup: BeautifulSoup, icon_class: str) -> Optional[float]:
    """Extract the first number from the container of the given icon class."""
    container = _icon_container(soup, icon_class)
    if not container:
        return None
    return _parse_num(container.get_text(" ", strip=True))


def _icon_int(soup: BeautifulSoup, icon_class: str) -> Optional[int]:
    val = _icon_num(soup, icon_class)
    return int(val) if val is not None else None


# ── Per-field extractors ──────────────────────────────────────────────────────

_TIPO_RE = re.compile(r"""['"]tipoDePropiedad['"]\s*:\s*['"]([^'"]+)['"]""")

def _get_type(soup: BeautifulSoup) -> Optional[str]:
    """Extract property type from the inline tracking script (dl = { ... }).
    Returns the value as-is — Zonaprop already uses Spanish ('Casa',
    'Departamento', 'PH', 'Local Comercial', 'Oficina', 'Terreno', …).
    """
    for script in soup.find_all("script"):
        text = script.string or ""
        m = _TIPO_RE.search(text)
        if m:
            val = m.group(1).strip()
            return val if val else None
    return None


def _get_covered_m2(soup: BeautifulSoup) -> Optional[float]:
    return _icon_num(soup, r"icon-scubierta")


def _get_total_m2(soup: BeautifulSoup) -> Optional[float]:
    return _icon_num(soup, r"icon-stotal")


def _get_ambientes(soup: BeautifulSoup, ld: dict) -> Optional[int]:
    val = _icon_int(soup, r"icon-ambiente")
    if val is not None:
        return val
    try:
        return int(float(str(ld["numberOfRooms"])))
    except (KeyError, ValueError, TypeError):
        return None


def _get_dormitorios(soup: BeautifulSoup, ld: dict) -> Optional[int]:
    val = _icon_int(soup, r"icon-dormitorio")
    if val is not None:
        return val
    try:
        return int(float(str(ld["numberOfBedrooms"])))
    except (KeyError, ValueError, TypeError):
        return None


def _get_banos(soup: BeautifulSoup, ld: dict) -> Optional[int]:
    val = _icon_int(soup, r"icon-bano")
    if val is not None:
        return val
    try:
        return int(float(str(ld["numberOfBathroomsTotal"])))
    except (KeyError, ValueError, TypeError):
        return None


def _get_toilettes(soup: BeautifulSoup) -> Optional[int]:
    return _icon_int(soup, r"icon-toilet")


def _get_floor(soup: BeautifulSoup) -> Optional[str]:
    val = _icon_num(soup, r"icon-piso")
    if val is not None:
        return str(int(val))
    return None


def _get_parking(soup: BeautifulSoup) -> Optional[bool]:
    val = _icon_num(soup, r"icon-cochera")
    if val is not None:
        return val > 0
    # Icon present but no number (e.g. "Cochera" without count)
    container = _icon_container(soup, r"icon-cochera")
    if container:
        text = container.get_text(" ", strip=True).lower()
        return "no" not in text
    return None


def _get_orientation(soup: BeautifulSoup) -> Optional[str]:
    container = _icon_container(soup, r"icon-orientaci")
    if container:
        text = container.get_text(" ", strip=True)
        m = re.search(r"\b(NE|NO|SE|SO|N|S|E|O)\b", text)
        if m:
            return _ORI_ABBR.get(m.group(1).upper(), m.group(1))
        for word in _ORI_WORDS:
            if word in text.lower():
                return word.capitalize()
    # Body text fallback
    body = soup.get_text(" ")
    for ori in ("Norte", "Sur", "Este", "Oeste", "Noreste", "Noroeste", "Sureste", "Suroeste"):
        if re.search(r"\b" + ori + r"\b", body, re.I):
            return ori
    return None


def _get_age_years(soup: BeautifulSoup) -> Optional[int]:
    container = _icon_container(soup, r"icon-antiguedad")
    if container:
        text = container.get_text(" ", strip=True)
        if re.search(r"estrenar", text, re.I):
            return 0
        val = _parse_num(text)
        if val is not None:
            return int(val)
    return None


def _get_condition(soup: BeautifulSoup) -> Optional[str]:
    # Key-value scan: look for "Estado" label next to a value
    for el in soup.find_all(string=re.compile(r"^estado$", re.I)):
        parent = el.find_parent()
        if parent:
            sibling = parent.find_next_sibling()
            if sibling:
                val = sibling.get_text(" ", strip=True)
                if val and len(val) < 40:
                    return val
    # Known condition strings in body text
    body = soup.get_text(" ")
    for cond in ("A estrenar", "Excelente estado", "Muy buen estado", "Buen estado", "Regular"):
        if re.search(re.escape(cond), body, re.I):
            return cond
    return None


def _get_expenses(soup: BeautifulSoup) -> Tuple[Optional[float], str]:
    el = (
        soup.find(attrs={"data-qa": re.compile(r"expens", re.I)}) or
        soup.find(class_=re.compile(r"expens|expensas", re.I))
    )
    if el:
        return _parse_price(el.get_text(" ", strip=True))
    return None, "ARS"


def _get_address(soup: BeautifulSoup, ld: dict) -> Optional[str]:
    # Primary: dedicated address class
    el = soup.find(class_=re.compile(r"location-address|LocationAddress", re.I))
    if el:
        return _clean_address(el.get_text(" ", strip=True))
    # Fallback: ld+json address object
    addr = ld.get("address")
    if isinstance(addr, dict):
        street_raw = (addr.get("streetAddress") or "").strip().rstrip(".")
        # Case 1 — clean: no commas → use the whole value (e.g. "Brasil al 300").
        # Case 2 — bloated: Zonaprop sometimes stuffs the entire address chain
        #   (postal code, province, country…) into streetAddress separated by
        #   commas (e.g. "Monseñor Larumbe 1059, B1640GXU Martínez, Provincia
        #   de Buenos Aires, Argentina, …").  Take only the first segment.
        street = street_raw.split(",")[0].strip() if "," in street_raw else street_raw

        # addressRegion holds the neighbourhood or municipality (e.g. "Martínez", "San Isidro")
        region = (addr.get("addressRegion") or "").strip()
        parts = [p for p in [street, region] if p]
        if parts:
            return _clean_address(", ".join(parts))
    elif isinstance(addr, str) and addr.strip():
        return _clean_address(addr.strip())
    return None


def _get_description(soup: BeautifulSoup, ld: dict) -> Optional[str]:
    # ld+json is the most complete source
    desc = (ld.get("description") or "").strip()
    if desc:
        return desc[:2000]
    # DOM: classic id, then data-qa
    el = (
        soup.find(id="longDescription") or
        soup.find(attrs={"data-qa": re.compile(r"descrip", re.I)})
    )
    if el:
        return el.get_text(" ", strip=True)[:2000]
    return None


def _get_amenities(soup: BeautifulSoup) -> Optional[List[str]]:
    seen: set = set()
    items: List[str] = []

    # Primary: scan section-main-features li items for non-standard icons.
    # Standard icons (numeric fields) are excluded; the rest are descriptive amenities.
    _STANDARD_ICONS = re.compile(
        r"icon-(stotal|scubierta|ambiente|dormitorio|bano|cochera|"
        r"antiguedad|orientaci|piso|toilete|toilet)",
        re.I,
    )
    features_section = soup.find(class_=re.compile(r"section-main-features", re.I))
    if features_section:
        for li in features_section.find_all("li"):
            icon_els = li.find_all(class_=re.compile(r"icon-", re.I))
            has_standard = any(
                _STANDARD_ICONS.search(" ".join(el.get("class", [])))
                for el in icon_els
            )
            if not has_standard and icon_els:
                t = li.get_text(" ", strip=True)
                if 1 < len(t) < 80 and t.lower() not in seen:
                    seen.add(t.lower())
                    items.append(t)

    # Fallback: look for dedicated amenity/tag elements
    if not items:
        for cls in ["amenity", "tag-item", "pill-item", "service-item"]:
            for el in soup.find_all(class_=re.compile(cls, re.I)):
                t = el.get_text(" ", strip=True)
                if 2 < len(t) < 60 and t.lower() not in seen:
                    seen.add(t.lower())
                    items.append(t)

    return items[:20] if items else None


def _get_images(soup: BeautifulSoup, ld: dict) -> Optional[List[str]]:
    """Extract gallery images from the rendered page.

    _scrape_detail waits until ≥2 imageGrid thumbnails have their src set
    before calling page.content(), so all images are present in the HTML.
    """
    def is_photo(url: str) -> bool:
        return bool(url and url.startswith("http") and not re.search(r"\.svg(\?|$)", url, re.I))

    # Primary: <img> tags inside the gallery portal.
    # wait_for_function in _scrape_detail ensures src is set on every image
    # before page.content() is called, so src is the only attribute we need.
    collected: List[str] = []
    media_root = soup.find(id="multimedia-content") or soup.find(id="new-gallery-portal")
    if media_root:
        for img in media_root.find_all("img"):
            src = img.get("src") or ""
            if is_photo(src) and src not in collected:
                collected.append(src)

    if collected:
        return _dedupe_images(collected)

    # Fallback: ld+json image field (single image or list)
    img_data = ld.get("image")
    if isinstance(img_data, str) and is_photo(img_data):
        return [img_data]
    if isinstance(img_data, list):
        return [i for i in img_data if isinstance(i, str) and is_photo(i)][:40]
    return None


def _dedupe_images(urls: List[str]) -> List[str]:
    """Deduplicate image URLs, preferring the highest resolution variant.

    Zonaprop CDN uses paths like /560x420/ or /1024x768/ in the URL.
    We normalise the size segment to a placeholder key and keep the largest.
    """
    seen: Dict[str, dict] = {}
    for url in urls:
        key = re.sub(r"/\d{2,4}x\d{2,4}/", "/SIZE/", url)
        size_m = re.search(r"/(\d+)x\d+/", url)
        size = int(size_m.group(1)) if size_m else 0
        if key not in seen or size > seen[key]["size"]:
            seen[key] = {"url": url, "size": size}
    return [v["url"] for v in seen.values()][:40]


def _get_publisher(soup: BeautifulSoup, ld: dict) -> Optional[str]:
    PLATFORM_NAMES = {"zonaprop", "argenprop", "mercadolibre", "properati"}

    def is_real_agent(name: str) -> bool:
        return bool(name and name.strip().lower() not in PLATFORM_NAMES and len(name.strip()) > 2)

    # Priority 1: ld+json — scan all blocks for an agent/org schema
    AGENT_TYPES = {"RealEstateAgent", "RealEstateAgency", "LocalBusiness", "Person"}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if not isinstance(data, dict):
                continue
            if data.get("@type") in AGENT_TYPES and is_real_agent(data.get("name", "")):
                return data["name"].strip()
            for field in ["author", "seller", "publisher", "agent", "broker"]:
                sub = data.get(field)
                if isinstance(sub, dict) and is_real_agent(sub.get("name", "")):
                    return sub["name"].strip()
        except Exception:
            pass

    # Priority 2: publisherData CSS-Module classes — use find(class_=re.compile())
    # because select_one('[class*="..."]') can choke on the hashed suffixes.
    for pattern in [
        r"publisherData-module__publisher-name",  # <h3> exact name element
        r"publisherData-module__name-container",   # wrapper div
        r"publisherData-module__card-columns",     # columns div
        r"publisherData-module__card-info",        # info div
    ]:
        el = soup.find(class_=re.compile(pattern, re.I))
        if el:
            name = el.get_text(" ", strip=True)
            if is_real_agent(name):
                return name

    # Priority 3: other DOM selectors
    for sel in [
        '[class*="react-publisher-card-property"]',
        '[data-qa="linkMicrositioAnunciante"]',
        '[data-qa="publisher-name"]',
        '[data-qa="ANUNCIANTE_NAME"]',
        '[class*="publisher-name"]',
        '[class*="publisherName"]',
        '[class*="real-estate-name"]',
        '[class*="agency-name"]',
    ]:
        el = soup.select_one(sel)
        if el:
            name = el.get_text(" ", strip=True)
            if is_real_agent(name):
                return name

    return None


def _get_publisher_phone(ld: dict) -> Optional[str]:
    raw = str(ld.get("telephone") or "").strip()
    return normalize_phone(raw)


def _get_published_at(soup: BeautifulSoup, ld: dict) -> Optional[str]:
    # ld+json datePosted (from House/Apartment block)
    pub = (ld.get("datePosted") or ld.get("datePublished") or "").strip()
    if pub:
        return pub[:10]
    # Inline script variable: const antiquity = 'Publicado hace X días'
    for script in soup.find_all("script"):
        text = script.string or ""
        m = re.search(r"const\s+antiquity\s*=\s*['\"]([^'\"]+)['\"]", text)
        if m:
            parsed = _parse_pub_date(m.group(1))
            if parsed:
                return parsed
    # Body text fallback (catches other "Publicado" mentions)
    m = re.search(r"Publicado.{0,80}", soup.get_text(" "), re.I)
    return _parse_pub_date(m.group(0)) if m else None


# ── Shared parsers ────────────────────────────────────────────────────────────

def _clean_address(address: str) -> str:
    if not address:
        return address
    address = re.sub(r"\s+al\s+", " ", address)
    parts = [p.strip() for p in address.split(",") if p.strip()]
    deduped = [parts[0]] if parts else []
    for part in parts[1:]:
        if part.lower() != deduped[-1].lower():
            deduped.append(part)
    return ", ".join(deduped)


def _parse_price(text: str) -> Tuple[Optional[float], str]:
    return parse_price(text)


def _parse_num(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"([\d]+(?:[.,]\d+)?)", text.replace(".", ""))
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None


def _parse_int(text: str) -> Optional[int]:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def _parse_pub_date(text: str) -> Optional[str]:
    if not text:
        return None
    text = text.strip()

    m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", text)
    if m:
        try:
            d = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            return d.isoformat()
        except ValueError:
            pass

    today = datetime.date.today()

    m = re.search(r"hace\s+(\d+)\s+h", text, re.IGNORECASE)
    if m:
        return today.isoformat()

    m = re.search(r"hace\s+(\d+)\s+d", text, re.IGNORECASE)
    if m:
        return (today - datetime.timedelta(days=int(m.group(1)))).isoformat()

    m = re.search(r"hace\s+(\d+)\s+sem", text, re.IGNORECASE)
    if m:
        return (today - datetime.timedelta(weeks=int(m.group(1)))).isoformat()

    m = re.search(r"hace\s+(\d+)\s+mes", text, re.IGNORECASE)
    if m:
        return (today - datetime.timedelta(days=int(m.group(1)) * 30)).isoformat()

    m = re.search(r"hace\s+(?:más\s+de\s+)?(\d+)\s+año", text, re.IGNORECASE)
    if m:
        return (today - datetime.timedelta(days=int(m.group(1)) * 365)).isoformat()

    if re.search(r"más de\s+\d*\s*año", text, re.IGNORECASE):
        return (today - datetime.timedelta(days=365)).isoformat()

    return None
