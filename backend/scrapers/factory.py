"""Scraper factory — returns the correct scraper by engine name."""

from config import settings
from .argenprop import ArgenpropScraper
from .mercadolibre import MercadoLibreScraper
from .remax import RemaxScraper
from .zonaprop import ZonapropScraper

_SCRAPERS = {
    "zonaprop": ZonapropScraper,
    "argenprop": ArgenpropScraper,
    "mercadolibre": MercadoLibreScraper,
    "remax": RemaxScraper,
}


def get_scraper(engine: str):
    """Return a scraper instance for the given search engine.

    Raises ValueError if the engine is not supported.
    """
    cls = _SCRAPERS.get(engine)
    if cls is None:
        raise ValueError(f"Unknown engine: {engine}. Valid: {settings.valid_engines}")
    return cls()
