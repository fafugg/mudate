"""Scrapers package — re-exports for backward compatibility.

The actual implementations live in:
- factory.py     — get_scraper()
- runner.py      — run_scrape(), make_run(), mark_cancelled()
- persistence.py — persist_listings()
"""


def __getattr__(name):
    if name == "get_scraper":
        from .factory import get_scraper as _f

        return _f
    if name in ("run_scrape", "make_run", "mark_cancelled"):
        from .runner import run_scrape as _rs, make_run as _mr, mark_cancelled as _mc

        _map = {"run_scrape": _rs, "make_run": _mr, "mark_cancelled": _mc}
        return _map[name]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    "get_scraper",
    "run_scrape",
    "make_run",
    "mark_cancelled",
]
