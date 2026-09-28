"""Tests for Zonaprop Cloudflare block detection.

Runs entirely offline — no browser is launched and no network request is made,
because the detection logic is factored out of the page navigation:

    _looks_like_challenge()   pure marker/status matching (this file's focus)
    _detect_block()           reads title/text/bootstrap flags from a page
    _raise_if_blocked()       raises ScrapeBlockedError with the reset hint
    _scrape_detail()          detail pages raise instead of returning {} silently
    _is_usable_detail()       whether an extraction is worth confirming
    _should_abort_on_detail_blocks()  whether a run's blocks are systemic

The marker sets were cross-checked against a live Zonaprop results page: a
healthy page is never flagged, because it is recognised as a results page
before any marker is considered.

Run from backend/:
    .venv/bin/python -m tests.test_block_detection
    .venv/bin/python tests/test_block_detection.py
"""

import asyncio
import contextlib
import logging
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapers.base import ScrapeBlockedError
from scrapers.zonaprop import (
    _CHALLENGE_MARKUP,
    _DETECT_SCRIPT,
    _PROFILE_DIR,
    _blocked_error,
    _detect_block,
    _is_usable_detail,
    _looks_like_challenge,
    _raise_if_blocked,
    _scrape_detail,
    _should_abort_on_detail_blocks,
    ZonapropScraper,
)


# A real Zonaprop results page (title/body taken from a live fetch).
REAL_PAGE_TITLE = (
    "60 Casas o PH de USD 300.000 a USD 310.001 en venta en "
    "San Isidro, GBA Norte - Zonaprop"
)
REAL_PAGE_SNIPPET = (
    "Comprar\nAlquilar\nBuscar inmobiliarias\n60 Casas o PH\n"
    "Casa en venta\n4 ambientes\n$ 320.000.000\n"
    "En un momento nos pondremos en contacto con el interesado."
)


class _FakePage:
    """Stand-in for a Playwright page.

    `_detect_block` / `_raise_if_blocked` only read `evaluate()`. `_scrape_detail`
    additionally reads `goto()`, `wait_for_selector()`, `wait_for_function()` and
    `content()` — the wait and evaluate counters let tests assert *when* the
    detection runs, which is what the short-circuit behaviour is about.
    """

    def __init__(self, payload=None, error: Exception = None, *,
                 goto_status=200, html="", goto_error: Exception = None):
        self._payload = payload
        self._error = error
        self._goto_status = goto_status
        self._html = html
        self._goto_error = goto_error
        self.goto_calls = 0
        self.evaluate_calls = 0
        self.wait_calls = 0

    async def evaluate(self, _script: str):
        self.evaluate_calls += 1
        if self._error:
            raise self._error
        return self._payload

    async def goto(self, _url, **_kwargs):
        self.goto_calls += 1
        if self._goto_error:
            raise self._goto_error
        return _resp(self._goto_status)

    async def wait_for_selector(self, _selector, **_kwargs):
        self.wait_calls += 1
        return None

    async def wait_for_function(self, _func, **_kwargs):
        self.wait_calls += 1
        return None

    async def content(self):
        return self._html


def _resp(status):
    """Stand-in for a Playwright Response (only `.status` is read)."""
    return types.SimpleNamespace(status=status)


# A rendered detail page: ld+json gives address/description/images, so
# _is_usable_detail passes and the marker inspection is skipped entirely.
HEALTHY_DETAIL_HTML = """<html><head>
<title>Casa en venta en Beccar, San Isidro - Zonaprop</title></head><body>
<script type="application/ld+json">{
  "@type": "House",
  "address": {"streetAddress": "Brasil al 300"},
  "description": "Casa con jardin y parrilla",
  "image": ["https://img.zonaprop.test/1.jpg", "https://img.zonaprop.test/2.jpg"]
}</script></body></html>"""

# No property markup at all: _extract_detail comes back empty, which is what
# makes stage two run.
CHALLENGE_DETAIL_HTML = """<html><head><title>Just a moment...</title></head>
<body><p>Verifying you are human</p></body></html>"""


def _healthy_payload(**over):
    """Payload shape as produced by _DETECT_SCRIPT on a real results page."""
    base = {
        "title": REAL_PAGE_TITLE,
        "text": REAL_PAGE_SNIPPET,
        "hasBootstrap": False,
        "isResults": True,
    }
    base.update(over)
    return base


class TestLooksLikeChallenge(unittest.TestCase):
    """Pure detection logic."""

    def test_block_status_always_wins(self):
        for status in (401, 403, 405, 408, 418, 429, 451, 503):
            with self.subTest(status=status):
                blocked, reason = _looks_like_challenge(
                    status, REAL_PAGE_TITLE, REAL_PAGE_SNIPPET, False, True
                )
                self.assertTrue(blocked)
                self.assertIn(f"HTTP {status}", reason)

    def test_ok_status_is_not_blocked(self):
        blocked, reason = _looks_like_challenge(
            200, REAL_PAGE_TITLE, REAL_PAGE_SNIPPET, False, True
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_missing_status_is_not_blocked(self):
        # Detection also runs with resp=None (e.g. the no-cards safety net).
        blocked, reason = _looks_like_challenge(
            None, REAL_PAGE_TITLE, "", False, False
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_english_challenge_title(self):
        blocked, reason = _looks_like_challenge(200, "Just a moment...", "", False, False)
        self.assertTrue(blocked)
        self.assertIn("desafío", reason)

    def test_spanish_challenge_title(self):
        blocked, _ = _looks_like_challenge(200, "Un momento...", "", False, False)
        self.assertTrue(blocked)

    def test_block_page_title(self):
        blocked, _ = _looks_like_challenge(
            200, "Attention Required! | Cloudflare", "", False, False
        )
        self.assertTrue(blocked)

    def test_challenge_markup_bootstrap(self):
        """`_cf_chl_opt` lives in a <script>, so it is a markup flag, not text."""
        blocked, reason = _looks_like_challenge(200, "", "", True, False)
        self.assertTrue(blocked)
        self.assertIn("bootstrap", reason)

    def test_challenge_text_markers(self):
        for marker in (
            "Verifying you are human. Our systems have detected unusual traffic",
            "Checking your browser before accessing the website",
            "Por favor, verifica que eres un humano para continuar",
            "This process is automatic. Your browser will redirect shortly.",
            # Observed live on a real 403 from zonaprop.com.ar
            "www.zonaprop.com.ar\nVerificación de seguridad en curso\n"
            "Este sitio web utiliza un servicio de seguridad para protegerse "
            "contra bots maliciosos.",
        ):
            with self.subTest(marker=marker[:30]):
                blocked, _ = _looks_like_challenge(
                    200, "", marker, False, False
                )
                self.assertTrue(blocked)

    def test_observed_live_challenge_page(self):
        """The exact challenge Zonaprop served during live testing.

        Reported as blocked even with a 200 status, purely from the title —
        which is what protects the case where Cloudflare answers 200 with the
        interstitial instead of 403.
        """
        blocked, reason = _looks_like_challenge(
            200,
            "Un momento…",
            "www.zonaprop.com.ar\nVerificación de seguridad en curso",
            False,
            False,
        )
        self.assertTrue(blocked)
        self.assertIn("Un momento", reason)

    def test_spanish_phrase_in_body_is_not_a_block(self):
        """`un momento` is matched against the TITLE only.

        Listing copy is full of it, so checking the body would flag healthy
        pages — this pins that behaviour down.
        """
        blocked, reason = _looks_like_challenge(
            200, "", "En un momento nos pondremos en contacto.", False, False
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_real_page_never_flagged(self):
        """The live page we verified against: no false positive."""
        blocked, _ = _looks_like_challenge(
            200, REAL_PAGE_TITLE, REAL_PAGE_SNIPPET, False, True
        )
        self.assertFalse(blocked)

    def test_results_page_guard_beats_markers(self):
        """Even a results page whose text contains a marker stays unflagged."""
        blocked, _ = _looks_like_challenge(
            200, REAL_PAGE_TITLE, "Just a moment...", False, True
        )
        self.assertFalse(blocked)

    def test_non_results_page_with_markers_is_flagged(self):
        blocked, _ = _looks_like_challenge(
            200, "Just a moment...", "Verifying you are human", False, False
        )
        self.assertTrue(blocked)


class TestDetectScript(unittest.TestCase):
    """The in-page script must stay in sync with the Python marker sets."""

    def test_markup_markers_serialized_into_script(self):
        for marker in _CHALLENGE_MARKUP:
            with self.subTest(marker=marker):
                self.assertIn(marker, _DETECT_SCRIPT)

    def test_script_checks_results_signals(self):
        # Signals verified live: listing cards, preloaded state, site title.
        self.assertIn("[data-posting-type]", _DETECT_SCRIPT)
        self.assertIn("__PRELOADED_STATE__", _DETECT_SCRIPT)
        self.assertIn("zonaprop", _DETECT_SCRIPT)


class TestBlockedError(unittest.TestCase):
    """The message must be actionable — it goes straight into the run banner."""

    def test_contains_profile_path_and_reset_command(self):
        msg = str(_blocked_error("HTTP 403"))
        self.assertIn(_PROFILE_DIR, msg)
        self.assertIn(f"rm -rf {_PROFILE_DIR}", msg)
        self.assertIn("HTTP 403", msg)
        self.assertIn("perfil", msg)

    def test_is_scrape_blocked_error(self):
        self.assertIsInstance(_blocked_error("HTTP 403"), ScrapeBlockedError)


class TestAsyncDetection(unittest.TestCase):
    """_detect_block / _raise_if_blocked against fake pages."""

    def test_raises_on_block_status(self):
        # A block status wins even on a page that looks like a results page.
        page = _FakePage(_healthy_payload())
        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(ScrapeBlockedError) as ctx:
                asyncio.run(_raise_if_blocked(page, _resp(403)))
        self.assertIn(_PROFILE_DIR, str(ctx.exception))
        self.assertIn("rm -rf", str(ctx.exception))
        self.assertTrue(any("rm -rf" in line for line in logs.output))

    def test_raises_on_challenge_interstitial(self):
        page = _FakePage(_healthy_payload(
            title="Just a moment...", text="cf bootstrap", isResults=False
        ))
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(ScrapeBlockedError):
                asyncio.run(_raise_if_blocked(page, _resp(200)))

    def test_raises_on_challenge_bootstrap(self):
        page = _FakePage(_healthy_payload(isResults=False, hasBootstrap=True))
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(ScrapeBlockedError):
                asyncio.run(_raise_if_blocked(page, _resp(200)))

    def test_silent_on_healthy_page(self):
        asyncio.run(_raise_if_blocked(_FakePage(_healthy_payload()), _resp(200)))

    def test_silent_when_payload_is_malformed(self):
        page = _FakePage(payload="not a dict")
        blocked, reason = asyncio.run(_detect_block(page, _resp(200)))
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_raises_on_block_status_when_inspection_fails(self):
        """Status is authoritative even with no readable page."""
        page = _FakePage(error=RuntimeError("Execution context was destroyed"))
        with self.assertLogs(level="ERROR"):
            with self.assertRaises(ScrapeBlockedError) as ctx:
                asyncio.run(_raise_if_blocked(page, _resp(403)))
        self.assertIn("HTTP 403", str(ctx.exception))

    def test_silent_when_inspection_fails(self):
        """Detection must never abort a healthy scrape by itself."""
        page = _FakePage(error=RuntimeError("Execution context was destroyed"))
        blocked, reason = asyncio.run(_detect_block(page, _resp(200)))
        self.assertFalse(blocked)
        self.assertEqual(reason, "")


class TestIsUsableDetail(unittest.TestCase):
    """Stage-two gate: decide whether an extraction is worth trusting."""

    def test_empty_extraction_is_not_usable(self):
        for detail in (None, {}, {"price": 100000, "currency": "USD"}):
            with self.subTest(detail=detail):
                self.assertFalse(_is_usable_detail(detail))

    def test_identity_field_makes_it_usable(self):
        for key, val in (
            ("address", "Brasil al 300"),
            ("description", "Casa con jardin"),
            ("images", ["https://img.zonaprop.test/1.jpg"]),
            ("type", "Casa"),
            ("covered_m2", 85.0),
            ("total_m2", 120.0),
            ("ambientes", 3),
            ("dormitorios", 2),
            ("real_estate", "REMAX"),
        ):
            with self.subTest(key=key):
                self.assertTrue(_is_usable_detail({key: val}))

    def test_empty_or_missing_values_do_not_count(self):
        # Card-level-only or blank fields must not trick us into skipping the
        # confirmation check on a page that extracted nothing.
        for detail in ({"images": []}, {"description": ""}, {"address": None}):
            with self.subTest(detail=detail):
                self.assertFalse(_is_usable_detail(detail))


class TestShouldAbortOnDetailBlocks(unittest.TestCase):
    """Threshold policy — must mirror the 50% rule used elsewhere."""

    def test_never_aborts_without_blocks(self):
        for blocked, attempted in ((0, 0), (0, 10), (0, 120)):
            with self.subTest(blocked=blocked, attempted=attempted):
                self.assertFalse(_should_abort_on_detail_blocks(blocked, attempted))

    def test_complete_block_always_aborts(self):
        # Even a tiny session: 1/1 and 2/2 are total refusals, so the floor
        # of 3 must not let them through.
        for blocked, attempted in ((1, 1), (2, 2), (120, 120)):
            with self.subTest(blocked=blocked, attempted=attempted):
                self.assertTrue(_should_abort_on_detail_blocks(blocked, attempted))

    def test_isolated_blocks_are_just_noise(self):
        # A handful of timeouts/transient failures must not kill a healthy run.
        for blocked, attempted in ((1, 10), (2, 10), (2, 100), (5, 120)):
            with self.subTest(blocked=blocked, attempted=attempted):
                self.assertFalse(_should_abort_on_detail_blocks(blocked, attempted))

    def test_systemic_blocks_abort(self):
        # Meets both clauses: >= 3 blocks AND >= 50% of attempts.
        for blocked, attempted in ((3, 6), (5, 10), (90, 120)):
            with self.subTest(blocked=blocked, attempted=attempted):
                self.assertTrue(_should_abort_on_detail_blocks(blocked, attempted))

    def test_more_than_half_but_under_three_does_not_abort(self):
        self.assertFalse(_should_abort_on_detail_blocks(2, 3))


class TestScrapeDetailBlocks(unittest.TestCase):
    """_scrape_detail must surface blocks instead of returning {} silently."""

    DETAIL_URL = "/casa-en-venta-beccar.html"

    def _run(self, page):
        return asyncio.run(_scrape_detail(page, self.DETAIL_URL))

    def test_raises_on_block_status(self):
        page = _FakePage(_healthy_payload(), goto_status=403, html=HEALTHY_DETAIL_HTML)
        with self.assertRaises(ScrapeBlockedError) as ctx:
            self._run(page)
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn(_PROFILE_DIR, str(ctx.exception))
        self.assertIn(f"rm -rf {_PROFILE_DIR}", str(ctx.exception))

    def test_short_circuits_before_waiting_on_block_status(self):
        # The whole point of checking the status first: a blocked detail page
        # must not burn the 8 s of selector waits that can never resolve.
        page = _FakePage(_healthy_payload(), goto_status=503, html=CHALLENGE_DETAIL_HTML)
        with self.assertRaises(ScrapeBlockedError):
            self._run(page)
        self.assertEqual(page.goto_calls, 1)
        self.assertEqual(page.wait_calls, 0)
        self.assertEqual(page.evaluate_calls, 0)

    def test_raises_on_challenge_served_with_200(self):
        # Soft block: 200 + interstitial + no property markup. Caught by the
        # confirmation pass, and it does log the reason with the reset command.
        page = _FakePage(
            _healthy_payload(
                title="Just a moment...", text="verifying you are human",
                isResults=False,
            ),
            goto_status=200, html=CHALLENGE_DETAIL_HTML,
        )
        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(ScrapeBlockedError) as ctx:
                self._run(page)
        self.assertIn(_PROFILE_DIR, str(ctx.exception))
        self.assertTrue(any("rm -rf" in line for line in logs.output))

    def test_healthy_page_is_returned_without_inspection(self):
        # The false-positive guarantee: a page that extracts property content
        # is never scanned for challenge markers, so listing text can never be
        # mistaken for a challenge.
        page = _FakePage(_healthy_payload(), goto_status=200, html=HEALTHY_DETAIL_HTML)
        detail = self._run(page)
        self.assertTrue(_is_usable_detail(detail))
        self.assertEqual(detail["address"], "Brasil 300")  # _clean_address strips "al"
        self.assertEqual(detail["description"], "Casa con jardin y parrilla")
        self.assertEqual(page.evaluate_calls, 0)

    def test_sparse_but_healthy_page_is_not_flagged(self):
        # Extraction found nothing, but the page is a genuine results/detail
        # page — the isResults guard wins and the listing is returned as-is.
        page = _FakePage(
            _healthy_payload(), goto_status=200,
            html=f"<html><body>{REAL_PAGE_SNIPPET}</body></html>",
        )
        detail = self._run(page)
        self.assertEqual(detail, {})
        self.assertEqual(page.evaluate_calls, 1)  # confirmation ran, then passed

    def test_unreadable_page_falls_back_to_empty_detail(self):
        # Detection must never abort a healthy scrape by itself: if the page
        # cannot be inspected we return the empty detail and let it through.
        page = _FakePage(
            _healthy_payload(), error=RuntimeError("Execution context was destroyed"),
            goto_status=200, html="",
        )
        self.assertEqual(self._run(page), {})

    def test_navigation_error_still_returns_empty_dict(self):
        # Non-block failures keep the original contract: log and continue.
        page = _FakePage(goto_error=RuntimeError("Timeout 30000ms exceeded"))
        with self.assertLogs(level="ERROR"):
            self.assertEqual(self._run(page), {})


class _FakeTab:
    """A detail tab: only route() in headless mode and close() are used."""

    def __init__(self, ctx: "_FakeContext"):
        self._ctx = ctx

    async def route(self, *_args, **_kwargs):
        return None

    async def close(self):
        self._ctx.tabs_closed += 1


class _FakeContext:
    def __init__(self):
        self.tabs_opened = 0
        self.tabs_closed = 0

    async def new_page(self):
        self.tabs_opened += 1
        return _FakeTab(self)

    async def close(self):
        return None


class _FakeBrowserPage:
    def __init__(self):
        self.context = _FakeContext()


def _raw_cards(n: int):
    return [
        {"id": f"1000{i}", "urlPath": f"/propiedad-{i}.html",
         "priceText": "U$S 150.000", "propType": "House"}
        for i in range(n)
    ]


def _block_idx(*indices):
    """Predicate that blocks only the cards at the given indices."""
    return lambda url: any(url.endswith(f"/propiedad-{i}.html") for i in indices)


class TestDetailBlockEscalation(unittest.TestCase):
    """Counter → threshold → escalate, exercised through real `scrape_search`.

    Only the browser and the two navigation entry points are stubbed; the real
    `_process_card`, gather, counter, threshold and escalation code all run.
    These cover the wiring that the pure-function tests cannot reach.
    """

    def _run(self, cards, block_url=None):
        @contextlib.asynccontextmanager
        async def _launch(_self, headless=True):
            yield _FakeBrowserPage()

        async def _collect(_page, _sf, _cb, cancel_check=None):
            # paging_info.total drives the existing partial-scrape warning.
            return cards, {"total": len(cards), "totalPages": 1}

        async def _detail(_page, url):
            if block_url and block_url(url):
                raise _blocked_error("HTTP 403")
            return {"address": "Brasil 300", "images": ["https://img.zonaprop.test/1.jpg"]}

        with patch.object(ZonapropScraper, "launch_browser", _launch), \
             patch("scrapers.zonaprop._collect_all_pages", _collect), \
             patch("scrapers.zonaprop._scrape_detail", _detail):
            return asyncio.run(
                ZonapropScraper().scrape_search(search_filter="-venta.html")
            )

    def test_all_detail_pages_blocked_raises_with_reset_hint(self):
        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaises(ScrapeBlockedError) as ctx:
                self._run(_raw_cards(4), block_url=lambda u: True)
        msg = str(ctx.exception)
        self.assertIn("4 de 4 páginas de detalle bloqueadas", msg)
        self.assertIn(f"rm -rf {_PROFILE_DIR}", msg)
        # The profile path must also be logged for `journalctl`, not only shown.
        self.assertTrue(any(_PROFILE_DIR in line for line in logs.output))

    def test_systemic_but_not_total_blocks_raise(self):
        # 3/4 satisfies both clauses (>=3 and >=50%) without being a total block.
        with self.assertRaises(ScrapeBlockedError) as ctx:
            self._run(_raw_cards(4), block_url=_block_idx(1, 2, 3))
        self.assertIn("3 de 4 páginas de detalle bloqueadas", str(ctx.exception))

    def test_isolated_blocks_save_the_run_and_keep_card_data(self):
        # 1/6 is noise: the run must complete, and the blocked listing must
        # still be returned with its card-level fields so db.json keeps it.
        results = self._run(_raw_cards(6), block_url=_block_idx(0))
        self.assertEqual(len(results), 6)
        blocked_card = next(r for r in results if r["url"].endswith("0.html"))
        self.assertEqual(blocked_card["price"], 150000.0)
        self.assertFalse(blocked_card.get("address"))  # detail was never fetched

    def test_no_blocks_returns_full_details(self):
        results = self._run(_raw_cards(2))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.get("address") == "Brasil 300" for r in results))

    def test_blocked_tabs_are_still_closed(self):
        # The except path must not bypass `finally: detail_page.close()` —
        # a leak of one tab per blocked card would kill a long run.
        opened = []

        @contextlib.asynccontextmanager
        async def _launch(_self, headless=True):
            ctx = _FakeContext()
            opened.append(ctx)
            yield types.SimpleNamespace(context=ctx)

        async def _collect(_page, _sf, _cb, cancel_check=None):
            return _raw_cards(3), {"total": 3, "totalPages": 1}

        async def _detail(_page, _url):
            raise _blocked_error("HTTP 403")

        with patch.object(ZonapropScraper, "launch_browser", _launch), \
             patch("scrapers.zonaprop._collect_all_pages", _collect), \
             patch("scrapers.zonaprop._scrape_detail", _detail):
            with self.assertRaises(ScrapeBlockedError):
                asyncio.run(ZonapropScraper().scrape_search(search_filter="-venta.html"))

        self.assertEqual(opened[0].tabs_opened, 3)
        # The finally must run for every blocked card — one leaked tab per
        # block would exhaust the browser on a long run.
        self.assertEqual(opened[0].tabs_closed, 3)


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.CRITICAL)
    unittest.main(verbosity=2)
