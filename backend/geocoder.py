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
import logging
import os
import re
import urllib.parse
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ── Endpoints ─────────────────────────────────────────────────────────────────

# Nominatim requires an identifying User-Agent per their usage policy.
_NOM_UA    = "CasaTracker/1.0 (personal-use geocoder)"
_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_OPENCAGE  = "https://api.opencagedata.com/geocode/v1/json"

_OPENCAGE_KEY: str = os.environ.get("OPENCAGE_API_KEY", "")

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

_NOM_INTERVAL = 1.1  # seconds between consecutive Nominatim request sends


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
            headers={"User-Agent": _NOM_UA},
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


# ── Public API ────────────────────────────────────────────────────────────────

async def geocode(address: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Resolve (lat, lng) for an Argentine address using a Nominatim cascade:

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

    # Steps 1-3: Nominatim cascade
    for i, variant in enumerate(vs, start=1):
        result = await _nominatim_rl(variant)
        if result:
            logger.info("Nominatim-%d %r -> %s", i, variant, result)
            return result

    # Step 4: OpenCage fallback
    if _OPENCAGE_KEY:
        result = await _opencage(vs[0])
        if result:
            logger.info("OpenCage %r -> %s", vs[0], result)
            return result

    logger.warning("All sources exhausted for %r", address)
    return None, None
