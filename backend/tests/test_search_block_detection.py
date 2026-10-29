"""Search-phase block detection for Argenprop and Remax.

The gap being pinned down: both engines swallowed refusals during the search
phase — Argenprop only logged a 403 and broke, Remax discarded the navigation
response entirely — so a blocked run reported "Listo. N propiedades
procesadas" with stale or zero data for that source and no way to tell.

The contract mirrors Zonaprop's, scaled to these engines: raise
``ScrapeBlockedError`` only when the block costs the WHOLE source (nothing
collected yet). Mid-pagination, the already-collected cards are kept — the
partial-scrape safeguard skips removals and the next run finishes the rest
(the Zonaprop deep-page lesson: partial data beats a fatally aborted source).
A healthy empty result page is never mistaken for a block.
"""

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from scrapers.argenprop import ArgenpropScraper
from scrapers.base import ScrapeBlockedError, challenge_marker_in_page
from scrapers.remax import RemaxScraper


class _Resp:
    def __init__(self, status):
        self.status = status


class _FakeDetailPage:
    async def close(self):
        pass


class _FakeContext:
    async def new_page(self):
        return _FakeDetailPage()


class _FakePage:
    """Minimal page double for the search phase.

    statuses: one HTTP status per goto() (last one repeats).
    marker:   value the challenge-marker inspection returns (None = healthy).
    evaluate: called with (script,) → ng_state; (script, markers) → marker.
    """

    def __init__(self, statuses, marker=None, ng_state=None):
        self._statuses = list(statuses)
        self._marker = marker
        self._ng_state = ng_state
        self.goto_count = 0
        self.context = _FakeContext()  # detail fetches use a separate page

    async def goto(self, url, **kwargs):
        idx = min(self.goto_count, len(self._statuses) - 1)
        self.goto_count += 1
        return _Resp(self._statuses[idx])

    async def evaluate(self, script, *args):
        if args:  # challenge_marker_in_page passes the marker list
            return self._marker
        return self._ng_state

    async def query_selector(self, selector):
        return None


def _launch_yielding(page):
    @asynccontextmanager
    async def fake_launch(self, headless=True):
        yield page
    return fake_launch


def _card(se_id="ap-1"):
    return {"id": se_id, "href": f"https://www.argenprop.com/{se_id}", "monto": "100000"}


def _remax_card(se_id="RM-1"):
    return {"search_engine_id": se_id, "price": 100000.0,
            "url": f"https://www.remax.com.ar/{se_id}", "_entity_id": "99"}


class TestChallengeMarkerInspection(unittest.TestCase):
    def test_reports_the_marker_found(self):
        async def run():
            return await challenge_marker_in_page(_FakePage([200], marker="_cf_chl_opt"))
        self.assertEqual(asyncio.run(run()), "_cf_chl_opt")

    def test_healthy_page_reports_none(self):
        async def run():
            return await challenge_marker_in_page(_FakePage([200]))
        self.assertIsNone(asyncio.run(run()))

    def test_inspection_failure_never_raises(self):
        class _Broken:
            async def evaluate(self, *a, **kw):
                raise RuntimeError("context destroyed")

        async def run():
            return await challenge_marker_in_page(_Broken())
        self.assertIsNone(asyncio.run(run()))


class TestArgenpropBlocked(unittest.TestCase):
    def setUp(self):
        self.scraper = ArgenpropScraper()
        self._sleep = patch("asyncio.sleep", new=AsyncMock())
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def _run(self, page):
        with patch.object(ArgenpropScraper, "launch_browser", _launch_yielding(page)), \
             patch("scrapers.argenprop._extract_cards_js", return_value=[]), \
             patch("scrapers.argenprop._extract_total_pages", return_value=1), \
             patch("scrapers.argenprop._extract_total_items", return_value=0):
            return asyncio.run(self.scraper.scrape_search("/casas-venta/"))

    def test_403_on_first_page_raises(self):
        with self.assertRaises(ScrapeBlockedError) as ctx:
            self._run(_FakePage([403]))
        self.assertIn("Argenprop bloqueado", str(ctx.exception))
        self.assertIn("HTTP 403", str(ctx.exception))

    def test_challenge_page_raises(self):
        with self.assertRaises(ScrapeBlockedError) as ctx:
            self._run(_FakePage([200], marker="_cf_chl_opt"))
        self.assertIn("desafío de Cloudflare (_cf_chl_opt)", str(ctx.exception))

    def test_healthy_empty_results_are_not_a_block(self):
        """Zero listings on a healthy page is an empty search, not a refusal."""
        self.assertEqual(self._run(_FakePage([200])), [])

    def test_mid_pagination_block_keeps_collected_cards(self):
        """Page 1 good, page 2 refused → keep page 1, no raise."""
        cards = [_card()]
        with patch.object(ArgenpropScraper, "launch_browser", _launch_yielding(_FakePage([200, 403]))), \
             patch("scrapers.argenprop._extract_cards_js", return_value=cards), \
             patch("scrapers.argenprop._extract_total_pages", return_value=2), \
             patch("scrapers.argenprop._extract_total_items", return_value=10), \
             patch("scrapers.argenprop._scrape_detail", new=AsyncMock(return_value={})):
            results = asyncio.run(
                self.scraper.scrape_search("/casas-venta/", existing_ids={"ap-1"})
            )
        self.assertEqual(len(results), 1, "partial cards must survive a mid-pagination block")
        self.assertEqual(results[0]["search_engine_id"], "ap-1")


class TestRemaxBlocked(unittest.TestCase):
    def setUp(self):
        self.scraper = RemaxScraper()
        self._sleep = patch("asyncio.sleep", new=AsyncMock())
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def _run(self, page, cards=None, total_pages=1):
        with patch.object(RemaxScraper, "launch_browser", _launch_yielding(page)), \
             patch.object(RemaxScraper, "_parse_ng_state",
                          return_value=(cards or [], total_pages, 0)):
            return asyncio.run(
                self.scraper.scrape_search("/propiedades/", existing_ids={"RM-1"})
            )

    def test_403_on_first_page_raises(self):
        with self.assertRaises(ScrapeBlockedError) as ctx:
            self._run(_FakePage([403]))
        self.assertIn("Remax bloqueado", str(ctx.exception))
        self.assertIn("HTTP 403", str(ctx.exception))

    def test_challenge_page_raises(self):
        with self.assertRaises(ScrapeBlockedError) as ctx:
            self._run(_FakePage([200], marker="cf-browser-verification"))
        self.assertIn("desafío de Cloudflare (cf-browser-verification)", str(ctx.exception))

    def test_mid_pagination_block_keeps_collected_cards(self):
        results = self._run(
            _FakePage([200, 403]), cards=[_remax_card()], total_pages=2
        )
        self.assertEqual(len(results), 1, "partial cards must survive a mid-pagination block")


if __name__ == "__main__":
    unittest.main()
