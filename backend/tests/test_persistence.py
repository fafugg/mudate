"""Tests for multi-engine persistence behavior."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scrapers.persistence import persist_listings, _merge


def _make_listing(se_id: str, engine: str, url: str, price: float) -> dict:
    return {
        "search_engine_id": se_id,
        "search_engine": engine,
        "url": url,
        "price": price,
        "currency": "USD",
        "address": "Test Address",
        "type": "Departamento",
        "ambientes": 3,
        "dormitorios": 2,
    }


def _make_session(session_id: str, sources: list) -> dict:
    return {
        "id": session_id,
        "search_sources": sources,
        "house_ids": [],
        "label": "Test",
        "created_at": "2024-01-01T00:00:00",
        "last_executed": None,
    }


class TestMultiEnginePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self.tmp.write(json.dumps({"users": {}, "houses": {}}))
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    @patch("storage.DB_PATH")
    def test_listings_from_two_engines_stored(self, mock_db_path):
        """Houses from zonaprop and argenprop are all stored in the DB."""
        mock_db_path.__str__ = lambda self: self.tmp.name

        session = _make_session(
            session_id="s1",
            sources=[{"engine": "zonaprop", "filter": "/zona1"}, {"engine": "argenprop", "filter": "/zona2"}],
        )

        listings = [
            _make_listing("zp-1", "zonaprop", "https://zp.com/1", 100000),
            _make_listing("zp-2", "zonaprop", "https://zp.com/2", 120000),
            _make_listing("ap-1", "argenprop", "https://ap.com/1", 90000),
        ]

        persist_listings(listings, session, "testuser")

        with open(self.tmp.name, "r") as f:
            db = json.load(f)

        user_hids = db["users"]["testuser"]["sessions"]["s1"]["house_ids"]
        self.assertEqual(len(user_hids), 3, "Expected 3 house IDs")

        engines = [db["houses"][hid]["search_engine"] for hid in user_hids]
        self.assertIn("zonaprop", engines)
        self.assertIn("argenprop", engines)

        zp_count = sum(1 for e in engines if e == "zonaprop")
        ap_count = sum(1 for e in engines if e == "argenprop")
        self.assertEqual(zp_count, 2)
        self.assertEqual(ap_count, 1)

    @patch("storage.DB_PATH")
    def test_dedup_across_engines_separate(self, mock_db_path):
        """Same search_engine_id in different engines are separate houses."""
        mock_db_path.__str__ = lambda self: self.tmp.name

        session = _make_session(
            session_id="s1",
            sources=[{"engine": "zonaprop", "filter": "/z1"}, {"engine": "argenprop", "filter": "/a1"}],
        )

        listings = [
            _make_listing("same-id", "zonaprop", "https://zp.com/1", 100000),
            _make_listing("same-id", "argenprop", "https://ap.com/1", 90000),
        ]

        persist_listings(listings, session, "testuser")

        with open(self.tmp.name, "r") as f:
            db = json.load(f)

        hids = db["users"]["testuser"]["sessions"]["s1"]["house_ids"]
        self.assertEqual(len(hids), 2, "Should have 2 distinct houses")

    @patch("storage.DB_PATH")
    def test_rerun_dedup_same_engine(self, mock_db_path):
        """Re-running with the same IDs does not duplicate."""
        mock_db_path.__str__ = lambda self: self.tmp.name

        session = _make_session(
            session_id="s1",
            sources=[{"engine": "zonaprop", "filter": "/z1"}],
        )

        listings = [_make_listing("zp-1", "zonaprop", "https://zp.com/1", 100000)]

        persist_listings(listings, session, "testuser")
        persist_listings(listings, session, "testuser")

        with open(self.tmp.name, "r") as f:
            db = json.load(f)

        hids = db["users"]["testuser"]["sessions"]["s1"]["house_ids"]
        self.assertEqual(len(hids), 1, "Should deduplicate on re-run")

    def test_merge_does_not_overwrite_lat_lng(self):
        """_merge does not overwrite lat/lng with scraped values (Option A)."""
        now = "2024-06-01T00:00:00"
        house = {
            "lat": -34.61,
            "lng": -58.44,
            "price": 100000,
            "currency": "USD",
            "last_updated": "2024-05-01T00:00:00",
        }

        listing = {
            "lat": 0.0,
            "lng": 0.0,
            "price": 110000,
            "currency": "USD",
        }

        _merge(house, listing, now)

        self.assertEqual(house["lat"], -34.61, "lat was overwritten")
        self.assertEqual(house["lng"], -58.44, "lng was overwritten")
        self.assertEqual(house["price"], 110000, "price not updated")
        self.assertEqual(house["last_updated"], now, "timestamp not updated")


if __name__ == "__main__":
    unittest.main()
