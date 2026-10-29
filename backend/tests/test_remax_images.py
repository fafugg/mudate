import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapers.persistence import persist_listings
from scrapers.remax import (
    MAX_GALLERY_IMAGES,
    _extract_remax_images,
    _find_remax_entity_id,
    _parse_remax_listing,
)


def _atomic_update_to(db_path, fn):
    with open(db_path, "r", encoding="utf-8") as file:
        db = json.load(file)
    fn(db)
    with open(db_path, "w", encoding="utf-8") as file:
        json.dump(db, file)


class TestRemaxGalleryImages(unittest.TestCase):
    def test_extracts_all_photos_and_deduplicates_resized_variants(self):
        photos = [
            {"rawValue": "listings/entity/photo-a"},
            {"rawValue": "listings/entity/photo-b"},
            {"rawValue": "listings/entity/1600x1200/photo-a.webp"},
            {"rawValue": "listings/entity/photo-b.webp"},
        ]

        images = _extract_remax_images(photos)

        self.assertEqual(len(images), 2)
        self.assertIn("/1600x1200/photo-a.webp", images[0])
        self.assertTrue(images[1].endswith("/listings/entity/photo-b.webp"))

    def test_prefers_explicit_original_url(self):
        images = _extract_remax_images([{
            "rawValue": "listings/entity/photo.webp",
            "originalUrl": "https://cdn.example/original/photo.webp",
        }])

        self.assertEqual(images, ["https://cdn.example/original/photo.webp"])

    def test_prefers_direct_url_over_generated_card_variant(self):
        images = _extract_remax_images([{
            "rawValue": "listings/entity/photo",
            "url": "https://cdn.example/photo.webp",
        }])

        self.assertEqual(images, ["https://cdn.example/photo.webp"])

    def test_applies_gallery_safety_cap(self):
        photos = [{"rawValue": f"listings/entity/photo-{i}"} for i in range(MAX_GALLERY_IMAGES + 5)]

        self.assertEqual(len(_extract_remax_images(photos)), MAX_GALLERY_IMAGES)

    def test_listing_exposes_entity_id_for_persistence(self):
        listing = _parse_remax_listing({
            "id": 123,
            "entityId": "entity-123",
            "slug": "casa-ejemplo",
            "photos": [{"rawValue": "listings/entity-123/photo-a"}],
        })

        self.assertEqual(listing["_entity_id"], "entity-123")

    def test_resolves_entity_id_only_for_matching_listing(self):
        state = {
            "listing": {"id": 123, "slug": "casa-ejemplo", "entityId": "entity-123"},
            "recommendations": [{"id": 456, "slug": "otra-casa", "entityId": "entity-456"}],
        }

        self.assertEqual(
            _find_remax_entity_id(state, "123", "https://www.remax.com.ar/listings/casa-ejemplo"),
            "entity-123",
        )
        self.assertIsNone(_find_remax_entity_id(
            state, "999", "https://www.remax.com.ar/listings/no-coincide"
        ))


class TestRemaxEntityPersistence(unittest.TestCase):
    def test_entity_id_is_retained_for_new_and_existing_houses(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as file:
            path = file.name
            json.dump({
                "users": {"u": {"sessions": {"s": {
                    "id": "s", "house_ids": ["existing"], "search_sources": [],
                }}}},
                "houses": {
                    "existing": {
                        "internal_id": "existing", "search_engine": "remax",
                        "search_engine_id": "old-id", "remax_entity_id": None,
                        "url": "https://www.remax.com.ar/listings/old",
                        "images": [], "status": "active", "created_at": "2026-01-01",
                        "previous_prices": [],
                    },
                },
            }, file)
        try:
            session = {"id": "s", "search_sources": [{"engine": "remax"}]}
            listings = [
                {
                    "search_engine": "remax", "search_engine_id": "old-id",
                    "_entity_id": "entity-old", "url": "https://www.remax.com.ar/listings/old",
                    "images": ["https://cdn.example/old.webp"],
                },
                {
                    "search_engine": "remax", "search_engine_id": "new-id",
                    "_entity_id": "entity-new", "url": "https://www.remax.com.ar/listings/new",
                    "images": ["https://cdn.example/new.webp"],
                },
            ]
            with patch(
                "scrapers.persistence.atomic_update",
                side_effect=lambda fn: _atomic_update_to(path, fn),
            ):
                persist_listings(listings, session, "u")

            with open(path, encoding="utf-8") as file:
                db = json.load(file)
            self.assertEqual(db["houses"]["existing"]["remax_entity_id"], "entity-old")
            created = next(h for h in db["houses"].values() if h.get("search_engine_id") == "new-id")
            self.assertEqual(created["remax_entity_id"], "entity-new")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
