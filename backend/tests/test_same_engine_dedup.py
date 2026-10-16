"""Tests for same-engine reactivation detection and merge."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from same_engine_dedup import _summarize, apply_same_engine_dedup, find_same_engine_duplicates
from schemas import SameEngineDedupHouseInfo, SameEngineDedupPreviewResponse


def _house(hid, *, status, url, price=100000, m2=50, created_at="2023-01-01T00:00:00",
           first_published_at=None, published_at=None, republish_count=0):
    return {
        "internal_id": hid,
        "search_engine_id": hid,
        "search_engine": "zonaprop",
        "url": url,
        "address": "Av. Corrientes 1234",
        "price": price,
        "currency": "USD",
        "total_m2": m2,
        "covered_m2": m2,
        "status": status,
        "created_at": created_at,
        "first_published_at": first_published_at,
        "published_at": published_at,
        "republish_count": republish_count,
        "last_updated": created_at,
        "previous_prices": [],
        "images": [],
    }


class TestFindDuplicates(unittest.TestCase):
    def setUp(self):
        self.session = {"id": "s1", "house_ids": ["old", "new"]}
        self.db = {"users": {}, "houses": {
            "old": _house("old", status="removed", url="https://zp.com/1"),
            "new": _house("new", status="active", url="https://zp.com/2"),
        }}

    def test_finds_removed_plus_new_pair(self):
        with patch("same_engine_dedup.read_db", return_value=self.db):
            groups = find_same_engine_duplicates(self.session, "u")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["old_id"], "old")
        self.assertEqual(groups[0]["new_id"], "new")

    def test_summary_exposes_original_date(self):
        self.db["houses"]["old"]["first_published_at"] = "2022-05-01"
        self.db["houses"]["new"]["published_at"] = "2024-03-01"
        with patch("same_engine_dedup.read_db", return_value=self.db):
            groups = find_same_engine_duplicates(self.session, "u")
        self.assertEqual(groups[0]["old_house"]["first_published_at"], "2022-05-01")
        # New house has no first_published_at → falls back to its published_at.
        self.assertEqual(groups[0]["new_house"]["first_published_at"], "2024-03-01")


class TestSummarizeNoneFields(unittest.TestCase):
    """Regression: houses with explicit None in nullable fields must not break
    response validation (the /same-engine-dedup/preview endpoint used to 500)."""

    def _house_with_nones(self, hid, status, address="Av. Corrientes 1234",
                          price=100000, m2=50):
        return {
            "internal_id": hid, "search_engine_id": hid, "search_engine": "zonaprop",
            "url": None, "address": address, "manual_address": None, "price": price,
            "currency": None, "total_m2": m2, "covered_m2": m2, "review": None,
            "images": None, "description": None, "published_at": None,
            "first_published_at": None, "created_at": None, "republish_count": None,
            "status": status, "last_updated": None, "previous_prices": [],
        }

    def test_summarize_coerces_none_for_non_optional_fields(self):
        summary = _summarize(self._house_with_nones("a", "removed", address=None))
        model = SameEngineDedupHouseInfo(**summary)  # must not raise
        self.assertEqual(model.description, "")
        self.assertEqual(model.url, "")
        self.assertEqual(model.images, [])
        self.assertEqual(model.currency, "USD")
        self.assertEqual(model.address, "")
        self.assertEqual(model.internal_id, "a")
        self.assertEqual(model.republish_count, 0)

    def test_preview_response_validates_with_none_fields(self):
        session = {"id": "s1", "house_ids": ["old", "new"]}
        db = {"users": {}, "houses": {
            "old": self._house_with_nones("old", "removed"),
            "new": self._house_with_nones("new", "active"),
        }}
        with patch("same_engine_dedup.read_db", return_value=db):
            groups = find_same_engine_duplicates(session, "u")
        self.assertTrue(groups, "the two houses should be detected as a reactivation")
        # This is exactly what the endpoint returns; it must validate.
        SameEngineDedupPreviewResponse(groups=groups)


class TestApplyMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _seed(self, db):
        with open(self.tmp.name, "w") as f:
            json.dump(db, f)

    def _apply(self, db, groups):
        self._seed(db)
        session = {"id": "s1"}

        def fake_update(fn):
            with open(self.tmp.name) as f:
                live = json.load(f)
            fn(live)
            with open(self.tmp.name, "w") as f:
                json.dump(live, f)

        with patch("same_engine_dedup.atomic_update") as mock_atomic:
            mock_atomic.side_effect = fake_update
            merged = apply_same_engine_dedup(session, "u", groups)
        with open(self.tmp.name) as f:
            return merged, json.load(f)

    def test_merge_keeps_earliest_date_and_counts_repost(self):
        db = {
            "users": {"u": {"sessions": {"s1": {"house_ids": ["old", "new"]}}}},
            "houses": {
                "old": _house("old", status="removed", url="https://zp.com/1",
                              first_published_at="2022-05-01", republish_count=0),
                "new": _house("new", status="active", url="https://zp.com/2",
                              published_at="2024-03-01", republish_count=0),
            },
        }
        groups = [{"old_id": "old", "new_id": "new"}]
        merged, out = self._apply(db, groups)

        self.assertEqual(merged, 1)
        self.assertNotIn("new", out["houses"], "new house must be deleted")
        keep = out["houses"]["old"]
        self.assertEqual(keep["first_published_at"], "2022-05-01")
        self.assertEqual(keep["republish_count"], 1)
        self.assertEqual(keep["search_engine_id"], "new", "search_engine_id transferred")
        self.assertNotIn("new", out["users"]["u"]["sessions"]["s1"]["house_ids"])

    def test_merge_carries_new_republish_count(self):
        db = {
            "users": {"u": {"sessions": {"s1": {"house_ids": ["old", "new"]}}}},
            "houses": {
                "old": _house("old", status="removed", url="https://zp.com/1",
                              first_published_at="2021-01-01", republish_count=2),
                "new": _house("new", status="active", url="https://zp.com/2",
                              published_at="2024-03-01", republish_count=3),
            },
        }
        groups = [{"old_id": "old", "new_id": "new"}]
        _, out = self._apply(db, groups)
        # old(2) + this re-post(1) + new(3)
        self.assertEqual(out["houses"]["old"]["republish_count"], 6)
        self.assertEqual(out["houses"]["old"]["first_published_at"], "2021-01-01")

    def test_merge_keeps_old_published_when_first_published_missing(self):
        """Legacy original keeps its published_at even after the new date is synced."""
        db = {
            "users": {"u": {"sessions": {"s1": {"house_ids": ["old", "new"]}}}},
            "houses": {
                "old": _house("old", status="removed", url="https://zp.com/1",
                              published_at="2020-02-01"),
                "new": _house("new", status="active", url="https://zp.com/2",
                              published_at="2024-03-01"),
            },
        }
        groups = [{"old_id": "old", "new_id": "new"}]
        _, out = self._apply(db, groups)
        self.assertEqual(out["houses"]["old"]["first_published_at"], "2020-02-01")
        self.assertEqual(out["houses"]["old"]["published_at"], "2024-03-01")


if __name__ == "__main__":
    unittest.main()
