"""Conservative, cached geocoding for Argentine property addresses.

Georef is the keyless default. Geoapify, OpenCage and a self-hosted or
third-party Nominatim instance are optional fallbacks. The public OSMF
Nominatim server is deliberately not used for automated/bulk geocoding.
"""

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx
from filelock import FileLock

from config import settings

logger = logging.getLogger(__name__)
GEOCACHE_PATH = settings.db_path + ".geocache.json"
_QUOTA_PATH = settings.db_path + ".geocode-quota.json"
_GEOREF = "https://apis.datos.gob.ar/georef/api/v2.1/direcciones"
_GEOAPIFY = "https://api.geoapify.com/v1/geocode/search"
_OPENCAGE = "https://api.opencagedata.com/geocode/v1/json"
_NEGATIVE_TTL = timedelta(days=7)
_CACHE_VERSION = 2
_MISS = object()
_geocache: Optional[dict] = None
_geocache_guard = threading.Lock()
_geocache_dirty = False
_http: Optional[httpx.AsyncClient] = None
_inflight = {}
_blocked_until = {}


@dataclass(frozen=True)
class GeocodeResult:
    lat: Optional[float] = None
    lng: Optional[float] = None
    provider: Optional[str] = None
    precision: Optional[str] = None
    # A provider outage/quota failure must not be persisted as a no-match.
    temporary_error: bool = False
    source_attribution: Optional[str] = None
    source_url: Optional[str] = None


class ProviderUnavailable(Exception):
    pass


def _get_http() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        # A configured endpoint must not silently redirect to a different
        # geocoding service (especially the public OSMF Nominatim instance).
        _http = httpx.AsyncClient(timeout=15.0, follow_redirects=False)
    return _http


def _reserve(provider: str, interval: float, minute_limit: int, hour_limit: int, day_limit: int) -> float:
    """Reserve a request across workers/restarts; return seconds to wait.

    Minute buckets include the oldest partially overlapping bucket, so all
    rolling limits are conservative. State is separate from the address cache.
    """
    now = time.time()
    minute = int(now // 60)
    with FileLock(_QUOTA_PATH + ".lock", timeout=10):
        try:
            with open(_QUOTA_PATH, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            raise ValueError("Invalid geocoding quota state")
        record = state.get(provider, {})
        buckets = {k: v for k, v in record.get("buckets", {}).items()
                   if int(k) >= minute - 1440}
        wait = max(0.0, float(record.get("last", 0)) + interval - now)
        if wait > 0:
            return wait
        if (sum(v for k, v in buckets.items() if int(k) >= minute - 1) >= minute_limit
                or sum(v for k, v in buckets.items() if int(k) >= minute - 60) >= hour_limit
                or sum(buckets.values()) >= day_limit):
            raise ProviderUnavailable("local quota reached")
        buckets[str(minute)] = buckets.get(str(minute), 0) + 1
        state[provider] = {"last": now, "buckets": buckets}
        tmp = _QUOTA_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _QUOTA_PATH)
    return 0.0


async def _request(provider: str, url: str, params: dict, limits: tuple) -> dict:
    try:
        if time.time() < _blocked_until.get(provider, 0):
            raise ProviderUnavailable("provider cooling down")
        while True:
            wait = _reserve(provider, *limits)
            if not wait:
                break
            await asyncio.sleep(wait)
        if time.time() < _blocked_until.get(provider, 0):
            raise ProviderUnavailable("provider cooling down")
        response = await _get_http().get(url, params=params, headers={"User-Agent": settings.nominatim_ua})
        if response.status_code in (403, 429):
            # Do not keep hammering a blocked or exhausted provider.
            _blocked_until[provider] = time.time() + 300
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, (dict, list)):
            raise ValueError("invalid response")
        return data
    except ProviderUnavailable:
        raise
    except (httpx.HTTPError, OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, httpx.RequestError) or (
            isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500
        ):
            _blocked_until[provider] = max(_blocked_until.get(provider, 0), time.time() + 60)
        # Do not log request URLs: they may contain an API key or address.
        logger.warning("Geocoding provider %s unavailable (%s)", provider, type(exc).__name__)
        raise ProviderUnavailable(provider) from exc


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _clean(address: str) -> str:
    # Never strip locality/partido: it is essential for disambiguation.
    address = re.sub(r"\b(?:piso|dpto|depto|apto|unidad)\.?\s+\w+", " ", address, flags=re.I)
    return re.sub(r"\s{2,}", " ", address).strip(" ,")


def _street_name(text: str) -> str:
    words = _normal(text).split()
    aliases = {"gral": "general", "avda": "avenida", "av": "avenida"}
    words = [aliases.get(w, w) for w in words]
    return " ".join(w for w in words if w not in {"avenida", "calle", "pasaje", "pje"})


def _address_street(text: str) -> str:
    # The last number is the house number; earlier numbers may be street names
    # such as "9 de Julio". Provider street names contain no house number.
    text = re.sub(r"\b\d+\w*\b(?!.*\b\d+\w*\b).*$", "", text)
    text = re.sub(r"\b(?:nro|numero|num)\.?\s*$", "", text, flags=re.I)
    return _street_name(text)


def _address_parts(address: str):
    parts = [p.strip() for p in _clean(address).split(",") if p.strip()]
    if len(parts) < 2:
        return None
    street = re.split(r"\b(?:entre|esquina)\b", parts[0], maxsplit=1, flags=re.I)[0]
    numbers = re.findall(r"\b\d+\b", street)
    if not numbers or not _address_street(street):
        return None
    context = [p for p in parts[1:] if _normal(p) not in {"argentina", "republica argentina", "gba", "gba norte", "gba sur", "gba oeste"}]
    if not context:
        return None
    return street, int(numbers[-1]), context


def _coords(lat, lng) -> Optional[Tuple[float, float]]:
    try:
        lat, lng = float(lat), float(lng)
        # Broad geographic guard, not a substitute for locality matching.
        if math.isfinite(lat) and math.isfinite(lng) and -56 <= lat <= -21 and -74 <= lng <= -53:
            return lat, lng
    except (TypeError, ValueError):
        pass
    return None


def _admin_names(candidate: dict) -> set:
    return {_normal((candidate.get(k) or {}).get("nombre", ""))
            for k in ("provincia", "departamento", "localidad", "localidad_censal")}


def _matches_context(context: list, names: set) -> bool:
    for part in context:
        value = _normal(part)
        if value in {"argentina", "republica argentina", "gba", "gba norte", "gba sur", "gba oeste"}:
            continue
        if value in {"caba", "capital federal"}:
            value = "ciudad autonoma de buenos aires"
        if value not in names:
            return False
    return True


def _province(context: list) -> Optional[str]:
    # Only infer provinces from explicit names, never from an assumed region.
    provinces = {"buenos aires", "catamarca", "chaco", "chubut", "cordoba", "corrientes", "entre rios", "formosa", "jujuy", "la pampa", "la rioja", "mendoza", "misiones", "neuquen", "rio negro", "salta", "san juan", "san luis", "santa cruz", "santa fe", "santiago del estero", "tierra del fuego", "tucuman"}
    for part in reversed(context):
        name = _normal(part)
        if name in {"caba", "capital federal", "ciudad autonoma de buenos aires"}:
            return "Ciudad Autónoma de Buenos Aires"
        if name in provinces:
            return part
    return None


def _choose_georef(data: dict, street: str, number: int, context: list, area: Optional[str]) -> Optional[GeocodeResult]:
    candidates = data.get("direcciones")
    if not isinstance(candidates, list):
        raise ProviderUnavailable("invalid Georef response")
    if len(candidates) >= 100:
        return None  # The response may be truncated; unseen matches could be ambiguous.
    expected_street = _address_street(street)
    province = _province(context)
    wanted_province = _normal(province) if province else None
    wanted_area = _normal(area) if area else None
    points = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if _street_name((item.get("calle") or {}).get("nombre", "")) != expected_street:
            continue
        if str((item.get("altura") or {}).get("valor")) != str(number):
            continue
        names = _admin_names(item)
        if wanted_province and _normal((item.get("provincia") or {}).get("nombre")) != wanted_province:
            continue
        if wanted_area and wanted_area not in names:
            continue
        if not _matches_context(context, names):
            continue
        coords = _coords((item.get("ubicacion") or {}).get("lat"), (item.get("ubicacion") or {}).get("lon"))
        if coords:
            points.add(coords)
    # Multiple plausible locations are not a safe automatic match.
    if len(points) != 1:
        return None
    lat, lng = points.pop()
    return GeocodeResult(lat, lng, "georef", "aproximada")


async def _georef(address: str) -> Optional[GeocodeResult]:
    parts = _address_parts(address)
    if not parts:
        return None
    street, number, context = parts
    province = _province(context)
    remaining = [p for p in context if _normal(p) not in {
        _normal(province or ""), "caba", "capital federal", "ciudad autonoma de buenos aires"}]
    area = remaining[-1] if remaining else None
    params = {"direccion": _clean(address).split(",", 1)[0].strip(), "max": 100}
    if province:
        params["provincia"] = province
    if area:
        params["departamento"] = area
    # A locality is not always a department; try both without dropping context.
    for field in (["departamento", "localidad"] if area else [None]):
        if field:
            params.pop("departamento", None)
            params.pop("localidad", None)
            params[field] = area
        data = await _request("georef", _GEOREF, params, (1.9, 38, 1900, 9000))
        result = _choose_georef(data, street, number, context, area)
        if result:
            return result
    return None


async def _geoapify(address: str) -> Optional[GeocodeResult]:
    if not settings.geoapify_api_key:
        return None
    parts = _address_parts(address)
    if not parts:
        return None
    data = await _request("geoapify", _GEOAPIFY, {
        "text": address, "filter": "countrycode:ar", "format": "json", "limit": 5,
        "lang": "es", "apiKey": settings.geoapify_api_key,
    }, (0.25, 240, 10000, 2800))
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ProviderUnavailable("invalid Geoapify response")
    street, number, context = parts
    valid = {}
    for item in results:
        rank = item.get("rank") or {}
        if (item.get("country_code") != "ar" or item.get("result_type") != "building"
                or str(item.get("housenumber")) != str(number)
                or _street_name(item.get("street", "")) != _address_street(street)
                or (rank.get("confidence") or 0) < 0.8):
            continue
        admins = {_normal(item.get(k, "")) for k in ("city", "county", "state", "suburb", "district")}
        if not _matches_context(context, admins):
            continue
        coords = _coords(item.get("lat"), item.get("lon"))
        source = item.get("datasource") or {}
        attribution = source.get("attribution")
        url = source.get("url")
        if coords and isinstance(attribution, str) and attribution.strip():
            try:
                parsed = urlparse(url) if isinstance(url, str) else None
            except ValueError:
                parsed = None
            safe_url = url if parsed and parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password else None
            valid[coords] = (attribution.strip()[:500], safe_url)
    if len(valid) != 1:
        return None
    (lat, lng), (attribution, url) = next(iter(valid.items()))
    return GeocodeResult(lat, lng, "geoapify", "aproximada", source_attribution=attribution, source_url=url)


async def _opencage(address: str) -> Optional[GeocodeResult]:
    if not settings.opencage_api_key:
        return None
    parts = _address_parts(address)
    if not parts:
        return None
    data = await _request("opencage", _OPENCAGE, {
        "q": address + ", Argentina", "key": settings.opencage_api_key,
        "limit": 5, "countrycode": "ar", "no_annotations": 1, "language": "es",
    }, (1.1, 50, 3000, 2000))
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise ProviderUnavailable("invalid OpenCage response")
    street, number, context = parts
    valid = set()
    for item in results:
        comp = item.get("components") or {}
        if (comp.get("country_code") != "ar" or str(comp.get("house_number")) != str(number)
                or _street_name(comp.get("road", "")) != _address_street(street)):
            continue
        admins = {_normal(comp.get(k, "")) for k in ("city", "town", "village", "county", "state", "suburb", "state_district")}
        if not _matches_context(context, admins):
            continue
        geo = item.get("geometry") or {}
        coords = _coords(geo.get("lat"), geo.get("lng"))
        if coords:
            valid.add(coords)
    if len(valid) != 1:
        return None
    lat, lng = valid.pop()
    return GeocodeResult(lat, lng, "opencage", "aproximada")


async def _nominatim(address: str) -> Optional[GeocodeResult]:
    url = settings.nominatim_url
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host == "nominatim.openstreetmap.org":
        logger.warning("Public Nominatim is not supported for automated geocoding; configure a private provider")
        return None
    parts = _address_parts(address)
    if not parts:
        return None
    data = await _request("nominatim", url, {"q": address + ", Argentina", "format": "json", "limit": 5,
                                               "countrycodes": "ar", "addressdetails": 1},
                          (1.1, 50, 3000, 10000))
    if not isinstance(data, list):
        raise ProviderUnavailable("invalid Nominatim response")
    street, number, context = parts
    valid = set()
    for item in data:
        comp = item.get("address") or {}
        admins = {_normal(comp.get(k, "")) for k in ("city", "town", "village", "county", "state", "suburb", "city_district")}
        if (str(comp.get("house_number")) != str(number) or _street_name(comp.get("road", "")) != _address_street(street)
                or not _matches_context(context, admins)):
            continue
        coords = _coords(item.get("lat"), item.get("lon"))
        if coords:
            valid.add(coords)
    if len(valid) != 1:
        return None
    lat, lng = valid.pop()
    return GeocodeResult(lat, lng, "nominatim", "aproximada")


def _geocache_load() -> dict:
    global _geocache
    if _geocache is None:
        with _geocache_guard:
            if _geocache is None:
                try:
                    with open(GEOCACHE_PATH, encoding="utf-8") as f:
                        data = json.load(f)
                    _geocache = data if isinstance(data, dict) else {}
                except (OSError, json.JSONDecodeError):
                    _geocache = {}
    return _geocache


def _geocache_key(address: str) -> str:
    return _clean(address).lower()


def _failure_is_fresh(entry: dict) -> bool:
    try:
        parsed = datetime.fromisoformat(entry.get("failed_at"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - parsed < _NEGATIVE_TTL
    except (TypeError, ValueError):
        return False


def _cached_result(entry):
    if isinstance(entry, dict):
        if "failed_at" in entry:
            # Old failures predate the new provider cascade; retry once.
            return GeocodeResult() if entry.get("version") == _CACHE_VERSION and _failure_is_fresh(entry) else _MISS
        coords = _coords(entry.get("lat"), entry.get("lng"))
        return GeocodeResult(*coords, entry.get("provider"), entry.get("precision"),
                             source_attribution=entry.get("source_attribution"), source_url=entry.get("source_url")) if coords else _MISS
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        coords = _coords(entry[0], entry[1])
        return GeocodeResult(*coords) if coords else _MISS
    return _MISS


def _geocache_put(cache: dict, key: str, result: GeocodeResult) -> GeocodeResult:
    global _geocache_dirty
    if result.temporary_error:
        # A forced retry of an old no-match must not leave the stale negative
        # behind after a provider outage.
        with _geocache_guard:
            entry = cache.get(key)
            if isinstance(entry, dict) and "failed_at" in entry:
                cache.pop(key, None)
                _geocache_dirty = True
        return result
    with _geocache_guard:
        if result.lat is not None:
            cache[key] = {"lat": result.lat, "lng": result.lng, "provider": result.provider, "precision": result.precision,
                          "source_attribution": result.source_attribution, "source_url": result.source_url}
        else:
            cache[key] = {"failed_at": datetime.now(timezone.utc).isoformat(), "version": _CACHE_VERSION}
        _geocache_dirty = True
    return result


def save_geocache() -> None:
    global _geocache_dirty
    with _geocache_guard:
        if not _geocache_dirty or _geocache is None:
            return
        keep = {k: v for k, v in _geocache.items()
                if not isinstance(v, dict) or "failed_at" not in v or _failure_is_fresh(v)}
        try:
            tmp = GEOCACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False)
            os.replace(tmp, GEOCACHE_PATH)
            _geocache_dirty = False
        except OSError as exc:
            logger.warning("No se pudo guardar el geocache: %s", exc)


async def _lookup(address: str, cache: dict, key: str) -> GeocodeResult:
    unavailable = False
    for fetch in (_georef, _geoapify, _opencage, _nominatim):
        try:
            result = await fetch(address)
        except ProviderUnavailable:
            unavailable = True
            continue
        except Exception as exc:
            # A malformed response from one provider must not prevent fallback.
            logger.warning("Geocoding provider %s failed (%s)", getattr(fetch, "__name__", "unknown"), type(exc).__name__)
            unavailable = True
            continue
        if result:
            return _geocache_put(cache, key, result)
    return _geocache_put(cache, key, GeocodeResult(temporary_error=unavailable))


async def geocode(address: str, force: bool = False) -> GeocodeResult:
    global _geocache_dirty
    if not address or not _clean(address):
        return GeocodeResult()
    cache = _geocache_load()
    key = _geocache_key(address)
    cached = _cached_result(cache.get(key))
    # Explicit retries bypass only negative entries, never successful cache.
    if cached is not _MISS and (not force or cached.lat is not None):
        return cached
    if force:
        with _geocache_guard:
            entry = cache.get(key)
            if isinstance(entry, dict) and "failed_at" in entry:
                cache.pop(key, None)
                _geocache_dirty = True
    inflight_key = key
    task = _inflight.get(inflight_key)
    if task is None:
        task = asyncio.create_task(_lookup(address, cache, key))
        _inflight[inflight_key] = task
        task.add_done_callback(lambda finished: _inflight.pop(inflight_key, None))
    return await asyncio.shield(task)
