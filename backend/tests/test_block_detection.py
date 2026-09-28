"""Tests for Zonaprop Cloudflare block detection.

Runs entirely offline — no browser is launched and no network request is made,
because the detection logic is factored out of the page navigation:

    _looks_like_challenge()   pure marker/status matching (this file's focus)
    _detect_block()           reads title/text/bootstrap flags from a page
    _raise_if_blocked()       raises ScrapeBlockedError with the reset hint

The marker sets were cross-checked against a live Zonaprop results page: a
healthy page is never flagged, because it is recognised as a results page
before any marker is considered.

Run from backend/:
    .venv/bin/python -m tests.test_block_detection
    .venv/bin/python tests/test_block_detection.py
"""

import asyncio
import logging
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapers.base import ScrapeBlockedError
from scrapers.zonaprop import (
    _CHALLENGE_MARKUP,
    _DETECT_SCRIPT,
    _PROFILE_DIR,
    _blocked_error,
    _detect_block,
    _looks_like_challenge,
    _raise_if_blocked,
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
    """Stand-in for a Playwright page: only `evaluate()` is used by detection."""

    def __init__(self, payload=None, error: Exception = None):
        self._payload = payload
        self._error = error

    async def evaluate(self, _script: str):
        if self._error:
            raise self._error
        return self._payload


def _resp(status):
    """Stand-in for a Playwright Response (only `.status` is read)."""
    return types.SimpleNamespace(status=status)


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


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.CRITICAL)
    unittest.main(verbosity=2)
