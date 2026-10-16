"""Tests for house_fields — the single source of house-record field rules."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from house_fields import (
    MUTABLE_LISTING_FIELDS,
    PUBLISH_FIELDS,
    advance_first_published,
    backfill_publish_fields,
    bump_republish,
    earliest_date,
    migrate_publish_fields,
    original_published_at,
    remember_price,
    sync_mutable_fields,
)


class TestEarliestDate(unittest.TestCase):
    def test_picks_earliest(self):
        self.assertEqual(earliest_date("2024-05-01", "2023-01-01", None), "2023-01-01")

    def test_normalizes_to_date_only(self):
        self.assertEqual(
            earliest_date("2024-05-01T10:30:00+00:00"),
            "2024-05-01",
        )

    def test_ignores_empty_values(self):
        self.assertEqual(earliest_date(None, "", "2024-01-01"), "2024-01-01")

    def test_all_empty_returns_none(self):
        self.assertIsNone(earliest_date(None, ""))


class TestOriginalPublishedAt(unittest.TestCase):
    def test_prefers_first_published(self):
        h = {"first_published_at": "2022-01-01", "published_at": "2023-01-01", "created_at": "2024-01-01"}
        self.assertEqual(original_published_at(h), "2022-01-01")

    def test_falls_back_to_published_then_created(self):
        self.assertEqual(original_published_at({"published_at": "2023-01-01", "created_at": "2024-01-01"}), "2023-01-01")
        self.assertEqual(original_published_at({"created_at": "2024-01-01T09:00:00"}), "2024-01-01")

    def test_empty(self):
        self.assertIsNone(original_published_at({}))


class TestBackfill(unittest.TestCase):
    def test_uses_published_at_when_present(self):
        h = {"published_at": "2023-03-04", "created_at": "2024-01-01"}
        backfill_publish_fields(h)
        self.assertEqual(h["first_published_at"], "2023-03-04")
        self.assertEqual(h["republish_count"], 0)

    def test_falls_back_to_created_at(self):
        h = {"created_at": "2024-01-01"}
        backfill_publish_fields(h)
        self.assertEqual(h["first_published_at"], "2024-01-01")

    def test_is_idempotent(self):
        h = {"first_published_at": "2020-01-01", "republish_count": 3}
        backfill_publish_fields(h)
        self.assertEqual(h["first_published_at"], "2020-01-01")
        self.assertEqual(h["republish_count"], 3)


class TestAdvanceFirstPublished(unittest.TestCase):
    def test_moves_earlier(self):
        h = {"first_published_at": "2024-01-01"}
        advance_first_published(h, "2023-01-01")
        self.assertEqual(h["first_published_at"], "2023-01-01")

    def test_never_moves_later(self):
        h = {"first_published_at": "2023-01-01"}
        advance_first_published(h, "2025-06-06")
        self.assertEqual(h["first_published_at"], "2023-01-01")

    def test_backfills_from_created_at_when_missing(self):
        h = {"created_at": "2024-01-01"}
        advance_first_published(h, "2025-01-01")
        self.assertEqual(h["first_published_at"], "2024-01-01")


class TestBumpRepublish(unittest.TestCase):
    def test_initializes_and_increments(self):
        h = {}
        bump_republish(h)
        self.assertEqual(h["republish_count"], 1)
        bump_republish(h, 2)
        self.assertEqual(h["republish_count"], 3)


class TestRememberPrice(unittest.TestCase):
    def test_records_previous_price(self):
        h = {"price": 100, "currency": "USD", "last_updated": "2024-01-01", "previous_prices": []}
        remember_price(h, 120, "2024-02-01")
        self.assertEqual(len(h["previous_prices"]), 1)
        self.assertEqual(h["previous_prices"][0]["price"], 100)

    def test_no_entry_when_price_unchanged(self):
        h = {"price": 100, "previous_prices": []}
        remember_price(h, 100, "2024-02-01")
        self.assertEqual(h["previous_prices"], [])


class TestSyncMutableFields(unittest.TestCase):
    def test_copies_non_null_mutable_fields(self):
        h = {"price": 100}
        sync_mutable_fields(h, {"price": 200, "address": "Calle 1", "lat": 0.0})
        self.assertEqual(h["price"], 200)
        self.assertEqual(h["address"], "Calle 1")
        self.assertNotIn("lat", h, "non-mutable fields must not be copied")

    def test_never_touches_publish_fields(self):
        h = {"first_published_at": "2020-01-01", "republish_count": 5, "price": 1}
        sync_mutable_fields(h, {"first_published_at": "2099-01-01", "republish_count": 0, "price": 2})
        self.assertEqual(h["first_published_at"], "2020-01-01")
        self.assertEqual(h["republish_count"], 5)

    def test_publish_fields_not_in_mutable_list(self):
        for field in PUBLISH_FIELDS:
            self.assertNotIn(field, MUTABLE_LISTING_FIELDS)


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self.tmp.write(json.dumps({"users": {}, "houses": {}}))
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _run(self, db):
        with open(self.tmp.name, "w") as f:
            json.dump(db, f)
        with patch("house_fields.atomic_update") as mock_atomic:
            def fake_update(fn):
                with open(self.tmp.name) as fh:
                    live = json.load(fh)
                fn(live)
                with open(self.tmp.name, "w") as fh:
                    json.dump(live, fh)
            mock_atomic.side_effect = fake_update
            updated = migrate_publish_fields()
        with open(self.tmp.name) as f:
            return updated, json.load(f)

    def test_backfills_legacy_houses(self):
        db = {"users": {}, "houses": {
            "a": {"created_at": "2023-01-01", "published_at": "2022-06-01"},
            "b": {"created_at": "2023-02-01"},
        }}
        updated, out = self._run(db)
        self.assertEqual(updated, 2)
        self.assertEqual(out["houses"]["a"]["first_published_at"], "2022-06-01")
        self.assertEqual(out["houses"]["b"]["first_published_at"], "2023-02-01")
        self.assertEqual(out["houses"]["a"]["republish_count"], 0)

    def test_is_idempotent(self):
        db = {"users": {}, "houses": {"a": {"created_at": "2023-01-01", "first_published_at": "2023-01-01", "republish_count": 0}}}
        updated, _ = self._run(db)
        self.assertEqual(updated, 0)


if __name__ == "__main__":
    unittest.main()
