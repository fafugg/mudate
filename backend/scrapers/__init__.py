"""Scrapers package — re-exports for backward compatibility.

The actual implementations live in:
- factory.py     — get_scraper()
- runner.py      — run_scrape(), make_run(), mark_cancelled()
- persistence.py — persist_listings()
"""

from .factory import get_scraper
from .runner import run_scrape, make_run, mark_cancelled

# Preserve the old names as aliases for any code that still imports them directly
_make_run = make_run
_mark_cancelled = mark_cancelled

__all__ = [
    "get_scraper",
    "run_scrape",
    "make_run",
    "_make_run",
    "mark_cancelled",
    "_mark_cancelled",
]
