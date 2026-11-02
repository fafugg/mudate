"""Offline regression tests for provider safety, caching and batch persistence."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import geocoder
import geocoding_tasks
import main
from fastapi import BackgroundTasks


def candidate(street="SANTA FE", number=260, province="Córdoba", department="Capital",
              locality="Córdoba", lat=-31.4078, lon=-64.2005):
    return {"calle": {"nombre": street}, "altura": {"valor": number},
            "provincia": {"nombre": province}, "departamento": {"nombre": department},
            "localidad": {"nombre": locality}, "localidad_censal": None,
            "ubicacion": {"lat": lat, "lon": lon}}


class GeorefTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_address_with_cross_streets_and_context(self):
        request = AsyncMock(return_value={"direcciones": [candidate()]})
        with patch.object(geocoder, "_request", request):
            result = await geocoder._georef("Av. Santa Fe nro 260 entre Santa Rosa y Colón, Capital, Córdoba")
        self.assertEqual(result.provider, "georef")
        self.assertEqual(result.precision, "aproximada")
        self.assertEqual(request.call_args.args[2]["departamento"], "Capital")
        self.assertIn("entre Santa Rosa", request.call_args.args[2]["direccion"])

    async def test_rejects_wrong_area_street_number_and_missing_coordinates(self):
        bad = [candidate(department="Otro"), candidate(street="OTRA"),
               candidate(number=261), candidate(lat=None), candidate(lon=0)]
        with patch.object(geocoder, "_request", AsyncMock(return_value={"direcciones": bad})):
            self.assertIsNone(await geocoder._georef("Santa Fe 260, Capital, Córdoba"))

    async def test_rejects_multiple_distinct_matching_points(self):
        with patch.object(geocoder, "_request", AsyncMock(return_value={"direcciones": [candidate(), candidate(lat=-31.40)]})):
            self.assertIsNone(await geocoder._georef("Santa Fe 260, Capital, Córdoba"))

    async def test_rejects_truncated_candidate_list(self):
        with patch.object(geocoder, "_request", AsyncMock(return_value={"direcciones": [candidate()] * 100})):
            self.assertIsNone(await geocoder._georef("Santa Fe 260, Capital, Córdoba"))

    async def test_locality_fallback_and_numeric_street(self):
        good = candidate(street="9 DE JULIO", number=150, department="San Isidro", locality="Martínez", province="Buenos Aires", lat=-34.48, lon=-58.5)
        request = AsyncMock(side_effect=[{"direcciones": []}, {"direcciones": [good]}])
        with patch.object(geocoder, "_request", request):
            result = await geocoder._georef("9 de Julio 150, Martínez, Buenos Aires")
        self.assertEqual(result.provider, "georef")
        self.assertEqual(request.call_args.args[2]["localidad"], "Martínez")

    async def test_requires_context_and_a_house_number(self):
        request = AsyncMock()
        with patch.object(geocoder, "_request", request):
            self.assertIsNone(await geocoder._georef("Santa Fe 260"))
            self.assertIsNone(await geocoder._georef("Santa Fe, Capital, Córdoba"))
        request.assert_not_awaited()

    async def test_rejects_other_explicit_locality(self):
        request = AsyncMock(return_value={"direcciones": [candidate(locality="Otra")]})
        with patch.object(geocoder, "_request", request):
            self.assertIsNone(await geocoder._georef("Santa Fe 260, Nueva Córdoba, Capital, Córdoba"))

    async def test_public_nominatim_is_never_used(self):
        settings = SimpleNamespace(nominatim_url="https://nominatim.openstreetmap.org/search")
        request = AsyncMock()
        with patch.object(geocoder, "settings", settings), patch.object(geocoder, "_request", request):
            self.assertIsNone(await geocoder._nominatim("Santa Fe 260, Capital, Córdoba"))
        request.assert_not_awaited()


class CacheAndProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cache = {}
        self.cache_patch = patch.object(geocoder, "_geocache", self.cache)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        geocoder._inflight.clear()

    async def test_outage_is_not_cached_or_treated_as_not_found(self):
        with patch.object(geocoder, "_georef", AsyncMock(side_effect=geocoder.ProviderUnavailable())), \
             patch.object(geocoder, "_geoapify", AsyncMock(return_value=None)), \
             patch.object(geocoder, "_opencage", AsyncMock(return_value=None)), \
             patch.object(geocoder, "_nominatim", AsyncMock(return_value=None)):
            result = await geocoder.geocode("Santa Fe 260, Capital, Córdoba")
        self.assertTrue(result.temporary_error)
        self.assertEqual(self.cache, {})

    async def test_falls_back_after_outage_and_caches_metadata(self):
        expected = geocoder.GeocodeResult(-34.5, -58.5, "geoapify", "aproximada")
        with patch.object(geocoder, "_georef", AsyncMock(side_effect=geocoder.ProviderUnavailable())), \
             patch.object(geocoder, "_geoapify", AsyncMock(return_value=expected)):
            result = await geocoder.geocode("Santa Fe 260, Capital, Córdoba")
            again = await geocoder.geocode("Santa Fe 260, Capital, Córdoba", force=True)
        self.assertEqual(result, expected)
        self.assertEqual(again, expected)
        self.assertEqual(next(iter(self.cache.values()))["provider"], "geoapify")

    async def test_malformed_provider_data_does_not_prevent_fallback(self):
        expected = geocoder.GeocodeResult(-34.5, -58.5, "geoapify", "aproximada")
        with patch.object(geocoder, "_georef", AsyncMock(side_effect=TypeError("bad data"))), \
             patch.object(geocoder, "_geoapify", AsyncMock(return_value=expected)):
            self.assertEqual(await geocoder.geocode("Santa Fe 260, Capital, Córdoba"), expected)

    async def test_old_failure_retried_but_fresh_failure_cached_and_force_retries(self):
        address = "Santa Fe 260, Capital, Córdoba"
        key = geocoder._geocache_key(address)
        self.cache[key] = {"failed_at": datetime.now(timezone.utc).isoformat()}
        fetch = AsyncMock(return_value=None)
        with patch.object(geocoder, "_georef", fetch), patch.object(geocoder, "_geoapify", AsyncMock(return_value=None)), \
             patch.object(geocoder, "_opencage", AsyncMock(return_value=None)), patch.object(geocoder, "_nominatim", AsyncMock(return_value=None)):
            await geocoder.geocode(address)
            await geocoder.geocode(address)
            self.assertEqual(fetch.await_count, 1)
            await geocoder.geocode(address, force=True)
            self.assertEqual(fetch.await_count, 2)

    async def test_failed_forced_retry_removes_stale_negative(self):
        address = "Santa Fe 260, Capital, Córdoba"
        key = geocoder._geocache_key(address)
        self.cache[key] = {"failed_at": datetime.now(timezone.utc).isoformat(), "version": 2}
        with patch.object(geocoder, "_georef", AsyncMock(side_effect=geocoder.ProviderUnavailable())), \
             patch.object(geocoder, "_geoapify", AsyncMock(return_value=None)), \
             patch.object(geocoder, "_opencage", AsyncMock(return_value=None)), \
             patch.object(geocoder, "_nominatim", AsyncMock(return_value=None)):
            self.assertTrue((await geocoder.geocode(address, force=True)).temporary_error)
            self.assertNotIn(key, self.cache)
            self.assertTrue((await geocoder.geocode(address)).temporary_error)

    async def test_concurrent_duplicate_lookups_share_the_request(self):
        expected = geocoder.GeocodeResult(-34.5, -58.5, "georef", "aproximada")
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch(_address):
            started.set()
            await release.wait()
            return expected

        mock = AsyncMock(side_effect=fetch)
        with patch.object(geocoder, "_georef", mock):
            first = asyncio.create_task(geocoder.geocode("Santa Fe 260, Capital, Córdoba"))
            await started.wait()
            second = asyncio.create_task(geocoder.geocode("Santa Fe 260, Capital, Córdoba", force=True))
            await asyncio.sleep(0)
            release.set()
            self.assertEqual(await first, expected)
            self.assertEqual(await second, expected)
        self.assertEqual(mock.await_count, 1)

    def test_reads_legacy_success_cache(self):
        result = geocoder._cached_result([-34.5, -58.5])
        self.assertEqual((result.lat, result.lng), (-34.5, -58.5))

    def test_cached_provider_attribution_is_preserved(self):
        result = geocoder.GeocodeResult(-34.5, -58.5, "geoapify", "aproximada",
                                       source_attribution="Source", source_url="https://example.com")
        geocoder._geocache_put(self.cache, "key", result)
        self.assertEqual(geocoder._cached_result(self.cache["key"]), result)

    async def test_geoapify_requires_confident_exact_address_and_location(self):
        settings = SimpleNamespace(geoapify_api_key="test-key")
        item = {"country_code": "ar", "result_type": "building", "housenumber": "260",
                "street": "Santa Fe", "city": "Córdoba", "county": "Capital", "state": "Córdoba",
                "lat": -31.4, "lon": -64.2, "rank": {"confidence": 0.9},
                "datasource": {"attribution": "© Test source", "url": "https://example.com"}}
        with patch.object(geocoder, "settings", settings), \
             patch.object(geocoder, "_request", AsyncMock(return_value={"results": [item]})):
            result = await geocoder._geoapify("Santa Fe 260, Capital, Córdoba")
            self.assertEqual(result.provider, "geoapify")
            self.assertEqual(result.source_attribution, "© Test source")
            item["rank"]["confidence"] = 0.2
            self.assertIsNone(await geocoder._geoapify("Santa Fe 260, Capital, Córdoba"))
            item["rank"]["confidence"] = 0.9
            item["county"] = "Otro"
            self.assertIsNone(await geocoder._geoapify("Santa Fe 260, Capital, Córdoba"))

    async def test_opencage_rejects_street_only_results(self):
        settings = SimpleNamespace(opencage_api_key="test-key")
        item = {"components": {"country_code": "ar", "road": "Santa Fe", "city": "Córdoba", "county": "Capital", "state": "Córdoba"},
                "geometry": {"lat": -31.4, "lng": -64.2}}
        with patch.object(geocoder, "settings", settings), \
             patch.object(geocoder, "_request", AsyncMock(return_value={"results": [item]})):
            self.assertIsNone(await geocoder._opencage("Santa Fe 260, Capital, Córdoba"))
            item["components"]["house_number"] = "260"
            self.assertEqual((await geocoder._opencage("Santa Fe 260, Capital, Córdoba")).provider, "opencage")

    def test_quota_is_persisted_and_blocks_before_limit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(geocoder, "_QUOTA_PATH", tmp + "/quota.json"):
            self.assertEqual(geocoder._reserve("georef", 0, 1, 100, 100), 0)
            with self.assertRaises(geocoder.ProviderUnavailable):
                geocoder._reserve("georef", 0, 1, 100, 100)
            with open(tmp + "/quota.json") as f:
                self.assertEqual(sum(json.load(f)["georef"]["buckets"].values()), 1)


class GeocodingTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_results_do_not_overwrite_new_address_or_coordinates(self):
        db = {"houses": {"h": {"address": "old", "lat": None, "lng": None}}}
        runs = {"r": {"status": "running"}}

        async def lookup(address, force=False):
            db["houses"]["h"]["manual_address"] = "new"
            return geocoder.GeocodeResult(-34.5, -58.5, "georef", "aproximada")

        with patch.object(geocoding_tasks, "read_db", return_value=db), \
             patch.object(geocoding_tasks, "atomic_update", side_effect=lambda fn: fn(db)), \
             patch.object(geocoding_tasks, "save_geocache"), \
             patch.object(geocoding_tasks, "geocode", side_effect=lookup):
            await geocoding_tasks.run_geocode(["h"], "r", runs)
        self.assertIsNone(db["houses"]["h"]["lat"])
        self.assertEqual(runs["r"]["updated_houses"], [])

    async def test_stale_results_do_not_overwrite_existing_coordinates(self):
        db = {"houses": {"h": {"address": "old", "lat": None, "lng": None}}}
        runs = {"r": {"status": "running"}}

        async def lookup(address, force=False):
            db["houses"]["h"].update(lat=-35.0, lng=-59.0)
            return geocoder.GeocodeResult(-34.5, -58.5, "georef", "aproximada")

        with patch.object(geocoding_tasks, "read_db", return_value=db), \
             patch.object(geocoding_tasks, "atomic_update", side_effect=lambda fn: fn(db)), \
             patch.object(geocoding_tasks, "save_geocache"), \
             patch.object(geocoding_tasks, "geocode", side_effect=lookup):
            await geocoding_tasks.run_geocode(["h"], "r", runs)
        self.assertEqual(db["houses"]["h"]["lat"], -35.0)
        self.assertEqual(runs["r"]["updated_houses"], [])

    async def test_temporary_errors_remain_retryable_and_report_partial(self):
        db = {"houses": {"h": {"address": "old", "lat": None, "lng": None, "geocode_failed": True}}}
        runs = {"r": {"status": "running"}}
        with patch.object(geocoding_tasks, "read_db", return_value=db), \
             patch.object(geocoding_tasks, "atomic_update", side_effect=lambda fn: fn(db)), \
             patch.object(geocoding_tasks, "save_geocache"), \
             patch.object(geocoding_tasks, "geocode", AsyncMock(return_value=geocoder.GeocodeResult(temporary_error=True))):
            await geocoding_tasks.run_geocode(["h"], "r", runs, force=True)
        self.assertFalse(db["houses"]["h"]["geocode_failed"])
        self.assertEqual(runs["r"]["status"], "partial")
        self.assertIn("reintentar", runs["r"]["message"])

    async def test_session_endpoint_retries_failed_address_and_persists_result(self):
        address = "Santa Fe 260, Capital, Córdoba"
        house = {"address": address, "lat": None, "lng": None, "geocode_failed": True}
        db = {"users": {"ana": {"sessions": {"s": {"id": "s", "house_ids": ["h"]}}}},
              "houses": {"h": house}}
        tasks = BackgroundTasks()
        main.runs.clear()
        self.addCleanup(main.runs.clear)
        with patch.object(main, "read_db", return_value=db), \
             patch.object(geocoding_tasks, "read_db", return_value=db), \
             patch.object(geocoding_tasks, "atomic_update", side_effect=lambda fn: fn(db)), \
             patch.object(geocoding_tasks, "save_geocache"), \
             patch.object(geocoder, "_geocache", {}), \
             patch.object(geocoder, "_request", AsyncMock(return_value={"direcciones": [candidate()]})):
            not_forced = await main.geocode_session("ana", "s", BackgroundTasks(), force=False)
            self.assertTrue(not_forced["already_done"])
            response = await main.geocode_session("ana", "s", tasks, force=True)
            await tasks()
            status = await main.get_run(response["run_id"])
        self.assertEqual(status["status"], "done")
        self.assertEqual(house["lat"], -31.4078)
        self.assertEqual(house["geocode_provider"], "georef")
        self.assertFalse(house["geocode_failed"])
        self.assertEqual(status["updated_houses"][0]["geocode_precision"], "aproximada")


if __name__ == "__main__":
    unittest.main()
