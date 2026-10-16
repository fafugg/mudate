"""Regression tests for MercadoLibre total/covered surface handling.

The bug being pinned down:
  * the ML search card's JSON-LD ``floorSize`` is the *covered* surface, but it
    was written to BOTH ``covered_m2`` and ``total_m2``;
  * persistence re-merges every non-None card field on each daily run, and
    known listings skip the detail page, so the fabricated total kept
    clobbering correctly extracted totals (e.g. 450 became 132 again).

A second, independent bug lived in the number parser: it swapped the decimal
comma before ``coerce_float`` (which expects Argentine formatting), so a page
value like ``202,4 m²`` was stored as ``2024``.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scrapers.mercadolibre import (
    _apply_specs,
    _extract_number,
    _parse_json_ld_listing,
    _specs_from_html,
)
from scrapers.persistence import persist_listings


def _atomic_update_to(db_path: str, fn):
    """Run fn(db) against the JSON file at db_path without file locking."""
    tmp_path = db_path + ".tmp"
    if os.path.exists(db_path):
        with open(db_path, "r", encoding="utf-8") as f:
            db = json.load(f)
    else:
        db = {"users": {}, "houses": {}}
    fn(db)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, db_path)


class TestSurfaceNumberParsing(unittest.TestCase):
    def test_argentine_formats_survive(self):
        self.assertEqual(_extract_number("252 m²"), 252.0)
        self.assertEqual(_extract_number("202,4 m²"), 202.4)
        self.assertEqual(_extract_number("1.234 m²"), 1234.0)
        self.assertEqual(_extract_number("1.234,5 m²"), 1234.5)
        self.assertIsNone(_extract_number("sin datos"))

    def test_apply_specs_maps_house_fields(self):
        result = {}
        _apply_specs(result, {
            "Superficie total": "450 m²",
            "Superficie cubierta": "132 m²",
            "Dormitorios": "4",
            "Baños": "3",
            "Cocheras": "2",
            "Antigüedad": "43 años",
            "Ambientes": "5",
            "Cantidad de pisos": "2",
        })
        self.assertEqual(result["total_m2"], 450.0)
        self.assertEqual(result["covered_m2"], 132.0)
        self.assertEqual(result["dormitorios"], 4)
        self.assertEqual(result["banos"], 3)
        self.assertTrue(result["parking"])
        self.assertEqual(result["age_years"], 43)
        self.assertEqual(result["ambientes"], 5)
        self.assertEqual(result["floor"], "2")

    def test_impossible_total_is_dropped(self):
        """total < covered must never be persisted."""
        result = {}
        _apply_specs(result, {"Superficie total": "186 m²", "Superficie cubierta": "204 m²"})
        self.assertIsNone(result["total_m2"])
        self.assertEqual(result["covered_m2"], 204.0)

    def test_equal_pair_is_preserved(self):
        """Some listings really do publish the same value in both rows."""
        result = {}
        _apply_specs(result, {"Superficie total": "414 m²", "Superficie cubierta": "414 m²"})
        self.assertEqual((result["total_m2"], result["covered_m2"]), (414.0, 414.0))

    def test_specs_from_html(self):
        html = """
        <table><tbody>
          <tr><th><div>Superficie total</div></th><td><div>252 m²</div></td></tr>
          <tr><th><div>Superficie cubierta</div></th><td><div>200 m²</div></td></tr>
        </tbody></table>
        """
        specs = _specs_from_html(html)
        self.assertEqual(specs["Superficie total"], "252 m²")
        self.assertEqual(specs["Superficie cubierta"], "200 m²")


class TestCardParser(unittest.TestCase):
    def test_card_never_invents_a_total(self):
        """floorSize is the covered surface; the card cannot know the total."""
        item = {
            "@type": "RealEstateListing",
            "offers": {"price": 400000, "priceCurrency": "USD",
                       "url": "https://casa.mercadolibre.com.ar/MLA-1-_JM"},
            "name": "Casa en venta en San Isidro",
            "address": {"addressLocality": "San Isidro", "addressRegion": "Bs.As."},
            "floorSize": {"@type": "QuantitativeValue", "value": 132, "unitCode": "MTK"},
            "seller": {"name": "MercadoLibre"},
        }
        card = _parse_json_ld_listing(item)
        self.assertEqual(card["covered_m2"], 132.0)
        self.assertIsNone(card["total_m2"])


class TestDailyRunDoesNotClobberTotal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self.tmp.close()
        self.username = "u"
        self.session_id = "s1"

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _session(self):
        return {
            "id": "s1",
            "search_sources": [{"engine": "mercadolibre", "filter": "/casas/venta/"}],
        }

    def _write_db(self, houses: dict, house_ids: list):
        db = {
            "users": {"u": {"sessions": {"s1": {
                "house_ids": house_ids,
                "label": "", "created_at": "", "last_executed": None,
                "search_sources": self._session()["search_sources"],
            }}}},
            "houses": houses,
        }
        with open(self.tmp.name, "w", encoding="utf-8") as f:
            json.dump(db, f)

    def _read_db(self):
        with open(self.tmp.name, "r", encoding="utf-8") as f:
            return json.load(f)

    @patch("scrapers.persistence.atomic_update")
    def test_known_listing_keeps_its_total(self, mock_atomic):
        """A daily run on a known listing must not reset total_m2 to covered."""
        mock_atomic.side_effect = lambda fn: _atomic_update_to(self.tmp.name, fn)

        self._write_db(
            {
                "h1": {
                    "search_engine": "mercadolibre",
                    "search_engine_id": "MLA-1",
                    "url": "https://casa.mercadolibre.com.ar/MLA-1-_JM",
                    "price": 400000.0, "currency": "USD",
                    "total_m2": 450.0, "covered_m2": 132.0,
                    "status": "active", "last_updated": "2026-01-01T00:00:00",
                    "previous_prices": [],
                    "review": "En duda", "notes": "keep me",
                }
            },
            ["h1"],
        )

        # What scrape_search produces for a KNOWN listing: card values only,
        # so total_m2 is None (the card cannot know the total).
        card = {
            "search_engine_id": "MLA-1",
            "search_engine": "mercadolibre",
            "url": "https://casa.mercadolibre.com.ar/MLA-1-_JM",
            "price": 405000.0, "currency": "USD",
            "type": "Casa", "address": "San Isidro",
            "covered_m2": 132.0, "total_m2": None,
            "price_per_m2": 3068.18,
        }
        persist_listings([card], self._session(), "u")

        house = self._read_db()["houses"]["h1"]
        self.assertEqual(house["total_m2"], 450.0, "total_m2 was clobbered by the card")
        self.assertEqual(house["covered_m2"], 132.0)
        self.assertEqual(house["price"], 405000.0, "price should still be refreshed")
        self.assertEqual(house["review"], "En duda", "user fields must be untouched")
        self.assertEqual(house["notes"], "keep me")

    @patch("scrapers.persistence.atomic_update")
    def test_new_listing_without_detail_has_no_total(self, mock_atomic):
        """If the detail page failed, do not store an invented equal pair."""
        mock_atomic.side_effect = lambda fn: _atomic_update_to(self.tmp.name, fn)

        self._write_db({}, [])
        card = {
            "search_engine_id": "MLA-2",
            "search_engine": "mercadolibre",
            "url": "https://casa.mercadolibre.com.ar/MLA-2-_JM",
            "price": 300000.0, "currency": "USD",
            "covered_m2": 100.0, "total_m2": None,
        }
        persist_listings([card], self._session(), "u")

        houses = self._read_db()["houses"]
        self.assertEqual(len(houses), 1)
        house = next(iter(houses.values()))
        self.assertIsNone(house["total_m2"])
        self.assertEqual(house["covered_m2"], 100.0)

    @patch("scrapers.persistence.atomic_update")
    def test_new_listing_with_detail_keeps_both(self, mock_atomic):
        """Card + detail is the normal path for a new listing."""
        mock_atomic.side_effect = lambda fn: _atomic_update_to(self.tmp.name, fn)

        self._write_db({}, [])
        card = {
            "search_engine_id": "MLA-3",
            "search_engine": "mercadolibre",
            "url": "https://casa.mercadolibre.com.ar/MLA-3-_JM",
            "price": 300000.0, "currency": "USD",
            "covered_m2": 132.0, "total_m2": None,
        }
        detail = {"total_m2": 450.0, "covered_m2": 132.0, "dormitorios": 4}
        card.update({k: v for k, v in detail.items() if v is not None})
        persist_listings([card], self._session(), "u")

        house = next(iter(self._read_db()["houses"].values()))
        self.assertEqual((house["total_m2"], house["covered_m2"]), (450.0, 132.0))


class TestDailyRunDoesNotClobberImages(unittest.TestCase):
    """The card thumbnail must never shrink a stored detail-page gallery."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _session(self, engine):
        return {
            "id": "s1",
            "search_sources": [{"engine": engine, "filter": "/casas/venta/"}],
        }

    def _write_db(self, houses: dict, house_ids: list):
        db = {
            "users": {"u": {"sessions": {"s1": {
                "house_ids": house_ids,
                "label": "", "created_at": "", "last_executed": None,
                "search_sources": self._session("mercadolibre")["search_sources"],
            }}}},
            "houses": houses,
        }
        with open(self.tmp.name, "w", encoding="utf-8") as f:
            json.dump(db, f)

    def _read_db(self):
        with open(self.tmp.name, "r", encoding="utf-8") as f:
            return json.load(f)

    def _known_house(self, engine, se_id, url, n_images):
        return {
            "search_engine": engine,
            "search_engine_id": se_id,
            "url": url,
            "price": 400000.0, "currency": "USD",
            "images": [f"http://img/{i}.jpg" for i in range(n_images)],
            "status": "active", "last_updated": "2026-01-01T00:00:00",
            "previous_prices": [],
            "review": "Interesante", "notes": "keep me",
        }

    @patch("scrapers.persistence.atomic_update")
    def test_ml_card_thumbnail_does_not_strip_gallery(self, mock_atomic):
        """A daily run must keep the stored 5 images, not drop to the card's 1."""
        mock_atomic.side_effect = lambda fn: _atomic_update_to(self.tmp.name, fn)

        self._write_db({"h1": self._known_house(
            "mercadolibre", "MLA-1", "https://casa.mercadolibre.com.ar/MLA-1-_JM", 5
        )}, ["h1"])

        # What scrape_search produces for a KNOWN listing: card values only —
        # the single JSON-LD thumbnail, no detail page fetched.
        card = {
            "search_engine_id": "MLA-1",
            "search_engine": "mercadolibre",
            "url": "https://casa.mercadolibre.com.ar/MLA-1-_JM",
            "price": 405000.0, "currency": "USD",
            "type": "Casa", "address": "San Isidro",
            "images": ["http://http2.mlstatic.com/D_thumb.jpg"],
            "price_per_m2": 3068.18,
        }
        persist_listings([card], self._session("mercadolibre"), "u")

        house = self._read_db()["houses"]["h1"]
        self.assertEqual(len(house["images"]), 5, "gallery was stripped to the card thumbnail")
        self.assertEqual(house["price"], 405000.0, "price should still be refreshed")
        self.assertEqual(house["review"], "Interesante")
        self.assertEqual(house["notes"], "keep me")

    @patch("scrapers.persistence.atomic_update")
    def test_argenprop_card_images_do_not_strip_gallery(self, mock_atomic):
        """Same guard for Argenprop, whose card carries up to 5 images."""
        mock_atomic.side_effect = lambda fn: _atomic_update_to(self.tmp.name, fn)

        self._write_db({"h1": self._known_house(
            "argenprop", "ap-1", "https://www.argenprop.com/casa-1", 8
        )}, ["h1"])

        card = {
            "search_engine_id": "ap-1",
            "search_engine": "argenprop",
            "url": "https://www.argenprop.com/casa-1",
            "price": 390000.0, "currency": "USD",
            "images": [f"http://card/{i}.jpg" for i in range(5)],
        }
        persist_listings([card], self._session("argenprop"), "u")

        house = self._read_db()["houses"]["h1"]
        self.assertEqual(len(house["images"]), 8, "gallery was clobbered by card images")

    @patch("scrapers.persistence.atomic_update")
    def test_new_listing_still_stores_detail_gallery(self, mock_atomic):
        """A new listing keeps the full gallery its detail page produced."""
        mock_atomic.side_effect = lambda fn: _atomic_update_to(self.tmp.name, fn)

        self._write_db({}, [])
        gallery = [f"http://img/{i}.jpg" for i in range(5)]
        card = {
            "search_engine_id": "MLA-2",
            "search_engine": "mercadolibre",
            "url": "https://casa.mercadolibre.com.ar/MLA-2-_JM",
            "price": 300000.0, "currency": "USD",
            "images": ["http://http2.mlstatic.com/D_thumb.jpg"],
        }
        card.update({"images": gallery})  # detail merge on a new listing
        persist_listings([card], self._session("mercadolibre"), "u")

        house = next(iter(self._read_db()["houses"].values()))
        self.assertEqual(house["images"], gallery)


if __name__ == "__main__":
    unittest.main()
