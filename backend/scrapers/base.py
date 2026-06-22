from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Set


class BaseScraper(ABC):
    """Clase base para scrapers de portales inmobiliarios.

    Cada scraper debe implementar scrape_search() y definir BASE_URL.
    """
    BASE_URL: str = ""
    delay: float = 2.0  # seconds between page requests

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

    def compute_price_per_m2(
        self, price: Optional[float], m2: Optional[float]
    ) -> Optional[float]:
        if price and m2 and m2 > 0:
            return round(price / m2, 2)
        return None
