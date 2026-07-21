"""Centralized configuration for Mudate.

All environment variables and magic constants live here.
Import `settings` anywhere you need config values.
"""

import os
from dataclasses import dataclass, field

_BASE = os.path.dirname(__file__)


@dataclass(frozen=True)
class Settings:
    # ── Database ──────────────────────────────────────────────────────────────
    db_path: str = field(
        default_factory=lambda: os.environ.get(
            "DB_PATH", os.path.join(_BASE, "..", "db.json")
        )
    )

    # ── Geocoding ─────────────────────────────────────────────────────────────
    opencage_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENCAGE_API_KEY", "")
    )
    geocode_batch_size: int = 10
    nominatim_interval: float = 1.1  # seconds between Nominatim sends
    nominatim_ua: str = "CasaTracker/1.0 (personal-use geocoder)"

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler_hour: int = 8
    scheduler_minute: int = 0
    scheduler_timezone: str = "America/Argentina/Buenos_Aires"

    # ── Scraping ──────────────────────────────────────────────────────────────
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    default_delay: float = 2.0  # seconds between page requests
    max_pages: int = 500  # safety cap for pagination

    # ── Admin ─────────────────────────────────────────────────────────────────
    max_upload_mb: int = 50
    run_prune_hours: int = 24

    # ── Valid search engines ──────────────────────────────────────────────────
    valid_engines: tuple = ("zonaprop", "argenprop", "mercadolibre", "remax")


settings = Settings()
