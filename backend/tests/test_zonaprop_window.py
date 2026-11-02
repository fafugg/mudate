"""The headed Zonaprop window must never be seen.

Cloudflare needs a real window, but a run must not appear on the desktop and
must not steal focus — including the 08:00 scheduled one. `--start-minimized`
alone is not enough (Playwright creates the window itself, and the switch is a
no-op on Wayland), so the window is parked off-screen and then minimized
through CDP. These cases pin both halves down without launching a browser.

Run from backend/:
    .venv/bin/python -m tests.test_zonaprop_window
    .venv/bin/python tests/test_zonaprop_window.py
"""

import asyncio
import logging
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import zonaprop  # noqa: E402


class _FakeSession:
    """Records CDP sends and answers Browser.getWindowForTarget."""

    def __init__(self, fail_on=None):
        self.sent = []
        self.detached = False
        self._fail_on = fail_on

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == self._fail_on:
            raise RuntimeError("CDP refused")
        if method == "Browser.getWindowForTarget":
            return {"windowId": 42}
        return {}

    async def detach(self):
        self.detached = True


class _FakeContext:
    def __init__(self, session):
        self._session = session
        self.sessions = 0

    async def new_cdp_session(self, page):
        self.sessions += 1
        return self._session


class _FakePage:
    def __init__(self, session):
        self.context = _FakeContext(session)


def _run(fn):
    return asyncio.new_event_loop().run_until_complete(fn)


class HiddenWindowArgs(unittest.TestCase):
    def test_headed_window_is_hidden_by_default(self):
        with mock.patch.object(zonaprop, "_HEADLESS", False), mock.patch.object(
            zonaprop, "_START_VISIBLE", False
        ):
            args = zonaprop._browser_args()
        self.assertIn("--window-position=-32000,-32000", args)
        self.assertIn("--start-minimized", args)

    def test_hidden_window_is_not_throttled(self):
        # Minimized counts as occluded; without these the page runs slow.
        with mock.patch.object(zonaprop, "_HEADLESS", False), mock.patch.object(
            zonaprop, "_START_VISIBLE", False
        ):
            args = zonaprop._browser_args()
        for flag in zonaprop._UNTHROTTLED_ARGS:
            self.assertIn(flag, args)

    def test_start_visible_shows_the_window(self):
        with mock.patch.object(zonaprop, "_HEADLESS", False), mock.patch.object(
            zonaprop, "_START_VISIBLE", True
        ):
            args = zonaprop._browser_args()
        self.assertNotIn("--window-position=-32000,-32000", args)
        self.assertNotIn("--start-minimized", args)

    def test_headless_keeps_container_flags_and_no_window_args(self):
        with mock.patch.object(zonaprop, "_HEADLESS", True), mock.patch.object(
            zonaprop, "_START_VISIBLE", False
        ):
            args = zonaprop._browser_args()
        self.assertIn("--no-sandbox", args)
        self.assertNotIn("--start-minimized", args)
        self.assertFalse(any(a.startswith("--window-position") for a in args))


class MinimizeWindow(unittest.TestCase):
    def test_minimizes_the_window_and_detaches(self):
        session = _FakeSession()
        _run(zonaprop._minimize_window(_FakePage(session)))

        methods = [m for m, _ in session.sent]
        self.assertEqual(
            methods, ["Browser.getWindowForTarget", "Browser.setWindowBounds"]
        )
        params = [p for m, p in session.sent if m == "Browser.setWindowBounds"][0]
        self.assertEqual(params, {"windowId": 42, "bounds": {"windowState": "minimized"}})
        self.assertTrue(session.detached, "the CDP session must not leak")

    def test_a_refusing_compositor_never_breaks_a_scrape(self):
        # Wayland has been known to ignore the request; that is cosmetic.
        for failing in ("Browser.getWindowForTarget", "Browser.setWindowBounds"):
            session = _FakeSession(fail_on=failing)
            _run(zonaprop._minimize_window(_FakePage(session)))  # must not raise
            self.assertTrue(session.detached)

    def test_no_cdp_session_never_breaks_a_scrape(self):
        page = _FakePage(_FakeSession())

        async def boom(_page):
            raise RuntimeError("no CDP here")

        page.context.new_cdp_session = boom
        _run(zonaprop._minimize_window(page))  # must not raise


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.CRITICAL)
    unittest.main(verbosity=2)
