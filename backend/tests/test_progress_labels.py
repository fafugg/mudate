"""Progress labels for the Zonaprop and Argenprop detail phase.

The regression: both scrapers labelled their progress "Verificando" for
already-known listings, but never defined ``is_known`` — the label was added
together with the parallel detail phase and the variable was left behind in
MercadoLibre and Remax only. Because the label is read inside
``if progress_callback:``, the NameError never fired in the offline tests
(which pass no callback) and only killed real runs, with
``name 'is_known' is not defined``.

Each case below feeds a callback and checks the two labels, so an undefined
(or always-false) ``is_known`` fails here instead of during a scrape.
"""

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from scrapers.argenprop import ArgenpropScraper
from scrapers.zonaprop import ZonapropScraper


class _FakeDetailPage:
    async def route(self, *args, **kwargs):
        return None

    async def close(self):
        return None


class _FakeContext:
    async def new_page(self):
        return _FakeDetailPage()


class _Resp:
    def __init__(self, status=200):
        self.status = status


class _FakePage:
    def __init__(self):
        self.context = _FakeContext()

    async def goto(self, url, **kwargs):
        return _Resp()

    async def evaluate(self, script, *args):
        return None

    async def query_selector(self, selector):
        return None


def _launch_yielding(page):
    @asynccontextmanager
    async def fake_launch(self, headless=True):
        yield page
    return fake_launch


def _labels(messages):
    return [m for m in messages]


class TestArgenpropProgressLabels(unittest.TestCase):
    def setUp(self):
        self.scraper = ArgenpropScraper()
        self._sleep = patch("asyncio.sleep", new=AsyncMock())
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def _run(self, existing_ids):
        messages = []
        card = {"id": "ap-1", "href": "https://www.argenprop.com/ap-1", "monto": "100000"}
        with patch.object(ArgenpropScraper, "launch_browser", _launch_yielding(_FakePage())), \
             patch("scrapers.argenprop._extract_cards_js", return_value=[card]), \
             patch("scrapers.argenprop._extract_total_pages", return_value=1), \
             patch("scrapers.argenprop._extract_total_items", return_value=1), \
             patch("scrapers.argenprop._scrape_detail", new=AsyncMock(return_value={})):
            asyncio.run(
                self.scraper.scrape_search(
                    "/casas-venta/",
                    progress_callback=lambda msg, done, total: messages.append(msg),
                    existing_ids=existing_ids,
                )
            )
        return messages

    def test_known_listing_says_verificando(self):
        self.assertTrue(
            any("Verificando" in m for m in self._run({"ap-1"})),
            _labels(self._run({"ap-1"})),
        )

    def test_unknown_listing_says_descargando(self):
        self.assertTrue(
            any("Descargando detalle" in m for m in self._run(set())),
            _labels(self._run(set())),
        )


class TestZonapropProgressLabels(unittest.TestCase):
    def setUp(self):
        self.scraper = ZonapropScraper()
        self._sleep = patch("asyncio.sleep", new=AsyncMock())
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def _run(self, existing_ids):
        messages = []
        cards = [{"id": "prop-1", "urlPath": "/prop-1"}]
        with patch.object(ZonapropScraper, "launch_browser", _launch_yielding(_FakePage())), \
             patch("scrapers.zonaprop._collect_all_pages",
                   new=AsyncMock(return_value=(cards, {}))), \
             patch("scrapers.zonaprop._scrape_detail", new=AsyncMock(return_value={})):
            asyncio.run(
                self.scraper.scrape_search(
                    "/casas-venta/",
                    progress_callback=lambda msg, done, total: messages.append(msg),
                    existing_ids=existing_ids,
                )
            )
        return messages

    def test_known_listing_says_verificando(self):
        self.assertTrue(
            any("Verificando" in m for m in self._run({"prop-1"})),
            _labels(self._run({"prop-1"})),
        )

    def test_unknown_listing_says_descargando(self):
        self.assertTrue(
            any("Descargando detalle" in m for m in self._run(set())),
            _labels(self._run(set())),
        )


if __name__ == "__main__":
    unittest.main()
