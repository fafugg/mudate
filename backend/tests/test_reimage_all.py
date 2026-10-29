import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.reimage_all import (
    _images_from_detail,
    _remax_filters_by_session,
    replacement,
    select_targets,
)


class TestRemaxBackfill(unittest.IsolatedAsyncioTestCase):
    async def test_uses_persisted_entity_id_for_api_backfill(self):
        calls = []

        async def fetcher(entity_id):
            calls.append(entity_id)
            return {"images": ["https://cdn.example/1.webp", "https://cdn.example/2.webp"]}

        images = await _images_from_detail(
            None,
            "remax",
            fetcher,
            {
                "url": "https://www.remax.com.ar/listings/example",
                "search_engine_id": "listing-id",
                "remax_entity_id": "entity-id",
            },
        )

        self.assertEqual(calls, ["entity-id"])
        self.assertEqual(len(images), 2)

    async def test_resolves_entity_id_for_legacy_house(self):
        calls = []

        class Page:
            async def goto(self, url, **kwargs):
                return type("Response", (), {"status": 200})()

            async def evaluate(self, script):
                return {"listing": {
                    "id": 123, "slug": "example", "entityId": "legacy-entity",
                }}

        async def fetcher(entity_id):
            calls.append(entity_id)
            return {"images": ["https://cdn.example/1.webp"]}

        images = await _images_from_detail(
            Page(),
            "remax",
            fetcher,
            {
                "url": "https://www.remax.com.ar/listings/example",
                "search_engine_id": "123",
            },
        )

        self.assertEqual(calls, ["legacy-entity"])
        self.assertEqual(images, ["https://cdn.example/1.webp"])

    def test_selects_remax_houses_for_backfill(self):
        targets = select_targets(
            {
                "house": {
                    "search_engine": "remax",
                    "url": "https://www.remax.com.ar/listings/example",
                    "images": ["https://cdn.example/thumb.webp"],
                },
            },
            ("remax",),
        )

        self.assertEqual([hid for hid, _ in targets["remax"]], ["house"])

    def test_never_replaces_with_empty_or_shorter_gallery(self):
        current = ["https://cdn.example/1.webp", "https://cdn.example/2.webp"]

        self.assertIsNone(replacement(current, []))
        self.assertIsNone(replacement(current, ["https://cdn.example/1.webp"]))

    def test_reads_saved_search_filters_for_legacy_records(self):
        filters = _remax_filters_by_session({
            "users": {"u": {"sessions": {"s": {
                "search_sources": [
                    {"engine": "remax", "filter": "/listings/buy?propertyType=9"},
                    {"engine": "argenprop", "filter": "/casas-venta"},
                    {"engine": "remax", "filter": "/listings/buy?propertyType=9"},
                ],
            }}}},
        })

        self.assertEqual(filters, {"s": ["/listings/buy?propertyType=9"]})


class TestRemaxSearchLookup(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_legacy_ids_from_search_results(self):
        from unittest.mock import patch
        from scrapers.remax import _resolve_remax_entity_ids_from_searches

        class Page:
            async def goto(self, url, **kwargs):
                return type("Response", (), {"status": 200})()

            async def evaluate(self, script, *args):
                return None

        listing = {"search_engine_id": "listing-id", "_entity_id": "entity-id"}
        with patch(
            "scrapers.remax.RemaxScraper._parse_ng_state",
            return_value=([listing], 1, 1),
        ):
            result = await _resolve_remax_entity_ids_from_searches(
                Page(), ["/listings/buy"], {"listing-id"}, delay=0
            )

        self.assertEqual(result, {"listing-id": "entity-id"})


if __name__ == "__main__":
    unittest.main()
