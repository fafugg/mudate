"""
Async geocoder for Argentine addresses.

Service cascade
───────────────
1. Nominatim (OSM) — primary; 1 req/s hard limit enforced by a pipelined
   rate-limiter (see below).
2. OpenCage          — fallback; requires OPENCAGE_API_KEY env var.
   Free tier: 2 500 req/day, 1 req/s.

Rate-limiting strategy ("pipelined" Nominatim)
────────────────────────────────────────────────
Classic approach: hold a lock FOR THE ENTIRE HTTP CALL.
  → next request must wait: (HTTP response time) + 1.1 s
  → 500 addresses × ~2.1 s = ~17 min

Pipelined approach (used here): hold the lock only long enough to claim a
send-time slot, then release it BEFORE the HTTP call.
  → HTTP calls for consecutive addresses overlap in time; only the 1.1 s
    *between sends* is enforced.
  → 500 addresses × 1.1 s = ~9 min  (≈ 2× faster)

This still strictly respects Nominatim's "max 1 req/s" policy because we
measure from when we *send* the request, not from when we *receive* it.
"""

import asyncio
import json
import logging
import os
import re
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import httpx

from config import settings

logger = logging.getLogger(__name__)

# ── Endpoints ─────────────────────────────────────────────────────────────────

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_OPENCAGE  = "https://api.opencagedata.com/geocode/v1/json"

_OPENCAGE_KEY: str = settings.opencage_api_key

# ── Persistent address → coords cache ─────────────────────────────────────────
# db.json keeps lat/lng per house, but the lookup itself was never cached:
# re-runs, forced geocoding and manual-address edits re-queried Nominatim for
# addresses already resolved (451 of the 2.338 addresses in the current DB are
# exact duplicates of another house's). Entries: [lat, lng] on success,
# {"failed_at": iso} on a full-cascade failure. The sidecar file lives next to
# db.json — inside the persisted volume — so it survives restarts.
# force=True bypasses reads (but still refreshes the entry).

GEOCACHE_PATH = settings.db_path + ".geocache.json"
_geocache: Optional[dict] = None
_geocache_guard = threading.Lock()
_geocache_dirty = False

# A failed lookup is remembered so a re-run doesn't burn 3+ requests on an
# address that does not resolve — but only for a while. Both fetchers swallow
# exceptions and return None, so a timeout or a 429 is indistinguishable from
# "no match": remembering it forever would leave a house without coordinates
# until someone clicked the force button. After the TTL the address is retried.
_NEGATIVE_TTL = timedelta(days=7)

# Returned by _cached_result() for "nothing usable in the cache".
_MISS = object()

# ── Shared async HTTP client (connection pooling + keep-alive) ────────────────

_http: Optional[httpx.AsyncClient] = None


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
        )
    return _http


# ── Pipelined Nominatim rate limiter ──────────────────────────────────────────
#
# _nom_sched_lock  — Semaphore(1): only one task schedules its send-time at a time
# _nom_next_send   — monotonic timestamp: earliest time the next request may be sent
#
# A task acquires _nom_sched_lock, sleeps until _nom_next_send (if needed),
# advances _nom_next_send by 1.1 s, releases the lock, and THEN makes the HTTP
# call.  While that HTTP call is in flight, the next task has already acquired
# the lock and is sleeping through its own 1.1 s window — so waits pipeline.

_nom_sched_lock: Optional[asyncio.Semaphore] = None
_nom_next_send:  float = 0.0

_NOM_INTERVAL = settings.nominatim_interval  # seconds between consecutive Nominatim request sends


def _get_nom_sched() -> asyncio.Semaphore:
    global _nom_sched_lock
    if _nom_sched_lock is None:
        _nom_sched_lock = asyncio.Semaphore(1)
    return _nom_sched_lock


# ── Address cleaning & variant generation ─────────────────────────────────────

_NOISE = [
    r"\bpiso\s+\w+",
    r"\bdpto?\.?\s+\w+",
    r"\bapto?\.?\s+\w+",
    r"\bunidad\s+\w+",
    r"\bof\.?\s+\w+",
    r"\blocal\s+\w+",
    r"\bGBA\s+\w+",
    r"\bzona\s+\w+",
    r"\bpartido\s+de\s+\w+",
]
_NOISE_RE  = re.compile("|".join(_NOISE), re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d+\w*\b")


def _clean(address: str) -> str:
    s = _NOISE_RE.sub(" ", address)
    return re.sub(r"\s{2,}", " ", s).strip(" ,")


def _variants(address: str) -> List[str]:
    """
    Up to three progressively simpler versions of the address:
      1. Cleaned address               e.g. "Av. Corrientes 1234, Palermo, CABA"
      2. Street number stripped        e.g. "Av. Corrientes, Palermo, CABA"
      3. First two comma-parts only    e.g. "Av. Corrientes 1234, Palermo"
    """
    seen:   set       = set()
    result: List[str] = []

    def add(s: str) -> None:
        s = re.sub(r"\s{2,}", " ", s).strip(" ,")
        if s and s not in seen:
            seen.add(s)
            result.append(s)

    cleaned = _clean(address)
    add(cleaned)
    add(_NUMBER_RE.sub("", cleaned))

    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if len(parts) >= 2:
        add(", ".join(parts[:2]))

    return result


# ── Low-level fetchers ────────────────────────────────────────────────────────

async def _nominatim_raw(query: str) -> Optional[Tuple[float, float]]:
    """Fire one Nominatim request.  Callers must go through _nominatim_rl."""
    params = urllib.parse.urlencode({
        "q":              f"{query}, Argentina",
        "format":         "json",
        "limit":          "1",
        "countrycodes":   "ar",
        "addressdetails": "0",
    })
    try:
        r = await _get_http().get(
            f"{_NOMINATIM}?{params}",
            headers={"User-Agent": settings.nominatim_ua},
        )
        data = r.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        logger.error("Nominatim %r -> %s: %s", query, type(e).__name__, e)
    return None


async def _nominatim_rl(query: str) -> Optional[Tuple[float, float]]:
    """
    Rate-limited Nominatim call.

    Acquires the scheduling lock only for the brief window needed to:
      1. sleep until the next permitted send time
      2. advance that time by _NOM_INTERVAL

    The HTTP request is made AFTER releasing the lock so subsequent tasks
    can pipeline their own sleep windows concurrently with this call.
    """
    global _nom_next_send
    sched = _get_nom_sched()

    async with sched:
        now  = asyncio.get_running_loop().time()
        wait = _nom_next_send - now
        if wait > 0:
            await asyncio.sleep(wait)
        _nom_next_send = asyncio.get_running_loop().time() + _NOM_INTERVAL
    # Lock released — next task can now schedule itself while this HTTP call runs
    return await _nominatim_raw(query)


async def _opencage(query: str) -> Optional[Tuple[float, float]]:
    """OpenCage — multi-source aggregator; requires OPENCAGE_API_KEY."""
    if not _OPENCAGE_KEY:
        return None
    params = urllib.parse.urlencode({
        "q":              f"{query}, Argentina",
        "key":            _OPENCAGE_KEY,
        "limit":          "1",
        "countrycode":    "ar",
        "no_annotations": "1",
        "language":       "es",
    })
    try:
        r = await _get_http().get(f"{_OPENCAGE}?{params}")
        results = r.json().get("results", [])
        if results:
            geo = results[0]["geometry"]
            return float(geo["lat"]), float(geo["lng"])
    except Exception as e:
        logger.error("OpenCage %r -> %s: %s", query, type(e).__name__, e)
    return None


# ── Address cache helpers ─────────────────────────────────────────────────────

def _geocache_load() -> dict:
    global _geocache
    if _geocache is None:
        with _geocache_guard:
            if _geocache is None:
                try:
                    with open(GEOCACHE_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    _geocache = data if isinstance(data, dict) else {}
                except (OSError, json.JSONDecodeError):
                    _geocache = {}
    return _geocache


def _geocache_key(address: str) -> str:
    """Canonical key: same cleaning the cascade applies, so cosmetic differences
    between two listings of the same address share one entry."""
    return _clean(address).lower()


def _failure_entry() -> dict:
    return {"failed_at": datetime.now(timezone.utc).isoformat()}


def _failure_is_fresh(entry: dict) -> bool:
    """True while a negative entry is younger than _NEGATIVE_TTL."""
    failed_at = entry.get("failed_at")
    try:
        parsed = datetime.fromisoformat(failed_at)
    except (TypeError, ValueError):
        return False  # unreadable timestamp → treat as expired and retry
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed < _NEGATIVE_TTL


def _cached_result(entry) -> object:
    """Translate a cache entry into a geocode result, or _MISS to re-query.

    Success entries are [lat, lng]. Failures are {"failed_at": iso} and only
    count while fresh; a bare None is a failure written before failures carried
    a timestamp, so it is retried and then rewritten in the new shape.
    """
    if isinstance(entry, dict):
        return (None, None) if _failure_is_fresh(entry) else _MISS
    if not entry:
        return _MISS
    try:
        return float(entry[0]), float(entry[1])
    except (TypeError, ValueError, IndexError):
        return _MISS  # corrupt entry → re-query


def save_geocache() -> None:
    """Persist the address cache (atomic replace); no-op when nothing changed.

    Called at the end of a run and every few flushes, not on every lookup:
    writing the whole file per address turned a 184-house run into 184 rewrites
    of a growing JSON file. Expired failures are dropped on the way out.
    """
    global _geocache_dirty
    with _geocache_guard:
        data = _geocache
        dirty = _geocache_dirty
        _geocache_dirty = False
    if not data or not dirty:
        return
    keep = {
        k: v for k, v in data.items()
        if not isinstance(v, dict) or _failure_is_fresh(v)
    }
    tmp = GEOCACHE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False)
        os.replace(tmp, GEOCACHE_PATH)
    except OSError as e:
        with _geocache_guard:
            _geocache_dirty = True  # retry on the next flush
        logger.warning("No se pudo guardar el geocache %s: %s", GEOCACHE_PATH, e)


# ── Public API ────────────────────────────────────────────────────────────────

async def geocode(address: str, force: bool = False) -> Tuple[Optional[float], Optional[float]]:
    """
    Resolve (lat, lng) for an Argentine address using a Nominatim cascade:

      Step 0 — persistent address cache (skipped when force=True)
      Step 1 — Nominatim, full cleaned address
      Step 2 — Nominatim, street number stripped
      Step 3 — Nominatim, first two address parts only
      Step 4 — OpenCage (only if OPENCAGE_API_KEY is set)

    All Nominatim calls go through the pipelined rate limiter which enforces
    ≥1.1 s between consecutive request sends regardless of how many addresses
    are being geocoded concurrently.
    """
    if not address:
        return None, None

    vs = _variants(address)
    if not vs:
        return None, None

    cache = _geocache_load()
    key = _geocache_key(address)
    if not force and key in cache:
        result = _cached_result(cache[key])
        if result is not _MISS:
            logger.debug("Geocache %r -> %s", address, result)
            return result

    # Steps 1-3: Nominatim cascade
    for i, variant in enumerate(vs, start=1):
        result = await _nominatim_rl(variant)
        if result:
            logger.info("Nominatim-%d %r -> %s", i, variant, result)
            return _geocache_put(cache, key, result)

    # Step 4: OpenCage fallback
    if _OPENCAGE_KEY:
        result = await _opencage(vs[0])
        if result:
            logger.info("OpenCage %r -> %s", vs[0], result)
            return _geocache_put(cache, key, result)

    logger.warning("All sources exhausted for %r", address)
    # Negative cache (with a TTL, see _NEGATIVE_TTL): retrying this exact test
    # right away would fail the same way, so the next run skips it instead of
    # spending 3+ rate-limited requests.
    return _geocache_put(cache, key, (None, None))


def _geocache_put(cache: dict, key: str, result: Tuple[Optional[float], Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    """Store a result in the in-memory cache and mark it dirty (see save_geocache)."""
    global _geocache_dirty
    cache[key] = [result[0], result[1]] if result[0] is not None else _failure_entry()
    with _geocache_guard:
        _geocache_dirty = True
    return result
