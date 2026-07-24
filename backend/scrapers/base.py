import re
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from playwright.async_api import async_playwright

from config import settings

# Shared constants
UA = settings.user_agent

# Anti-detection script used by all scrapers
_INIT_SCRIPT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


class BaseScraper(ABC):
    """Clase base para scrapers de portales inmobiliarios.

    Cada scraper debe implementar scrape_search() y definir BASE_URL.
    """
    BASE_URL: str = ""
    delay: float = settings.default_delay

    @abstractmethod
    async def scrape_search(
        self,
        search_filter: str,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        existing_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return list of house dicts for all pages of the given filter.

        existing_ids: set of search_engine_id values already in the DB.
        When provided, detail pages are skipped for known houses — only
        card-level data (price, m², rooms) is returned so the caller can
        detect price changes without the cost of a full page load.
        """
        pass

    @asynccontextmanager
    async def launch_browser(self, headless: bool = True):
        """Launch a configured Playwright Chromium browser with anti-detection.

        Usage:
            async with self.launch_browser() as page:
                await page.goto(url)
                ...
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 900},
                locale="es-AR",
            )
            page = await context.new_page()
            await page.add_init_script(_INIT_SCRIPT)
            try:
                yield page
            finally:
                await context.close()

    def compute_price_per_m2(
        self, price: Optional[float], m2: Optional[float]
    ) -> Optional[float]:
        if price and m2 and m2 > 0:
            return round(price / m2, 2)
        return None


# ── Shared utility functions ──────────────────────────────────────────────────


def coerce_float(v) -> Optional[float]:
    """Convert a value to float, handling Argentine number formatting (dots → nothing, comma → dot).

    If the value is already numeric (int/float), it is returned as-is without
    the string-based formatting, which would corrupt standard decimal numbers.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(".", "").replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def coerce_int(v) -> Optional[int]:
    """Convert a value to int via coerce_float."""
    f = coerce_float(v)
    return int(f) if f is not None else None


def parse_price(text: str) -> Tuple[Optional[float], str]:
    """Parse a price string like 'U$S 150.000' or '$ 250.000' into (amount, currency).

    Detects USD if text contains 'U$S', 'USD', or 'US$'; otherwise assumes ARS.
    """
    if not text:
        return None, "USD"
    upper = text.upper()
    currency = "USD" if ("U$S" in upper or "USD" in upper or "US$" in upper) else "ARS"
    cleaned = re.sub(r"[A-Za-z$U\s]", "", text).replace(".", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    if m:
        try:
            return float(m.group()), currency
        except ValueError:
            pass
    return None, currency


def normalize_phone(raw: str) -> Optional[str]:
    """Normalize an Argentine phone number to '+54 XXXX...' format.

    Strips non-digits, removes country code (54) and leading 9.
    Returns None if the result is empty.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("54"):
        digits = digits[2:]
    if digits.startswith("9"):
        digits = digits[1:]
    return f"+54 {digits}" if digits else None
