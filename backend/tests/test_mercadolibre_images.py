"""Regression tests for MercadoLibre gallery extraction and redirect guards.

Two bugs are pinned down here:

  * MercadoLibre migrated its PDP gallery from ``img.gallery-image__image`` to
    ``img.ui-pdp-gallery__figure__image`` and the scraper kept reading only the
    legacy class, so every listing on the new layout yielded zero images.  The
    two layouts are reproduced below as static HTML, so the test needs no
    network: it renders the markup each layout emits and runs the very same JS
    the scraper evaluates against the page.

  * A finalized listing hydrates the PDP and only then soft-navigates to a
    search results page whose URL carries the original one in its
    ``#redirectedFromVip=`` fragment.  The redirect guard did a plain substring
    test, so the MLA id inside the fragment kept the guard quiet and the search
    page's first card image was stored as the listing's gallery.

Run:  python tests/test_mercadolibre_images.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapers.mercadolibre import _GALLERY_IMAGES_JS, _redirected_away

try:
    from playwright.async_api import async_playwright

    _PLAYWRIGHT = True
except ImportError:  # pragma: no cover - playwright is a hard dependency
    _PLAYWRIGHT = False


# The current layout: src is the -O.webp preview, data-zoom the -F.webp photo.
NEW_LAYOUT = """
<div class="ui-pdp-gallery">
  <img class="ui-pdp-image ui-pdp-gallery__figure__image"
       src="https://http2.mlstatic.com/AAA-O.webp"
       data-zoom="https://http2.mlstatic.com/AAA-F.webp">
  <img class="ui-pdp-image ui-pdp-gallery__figure__image"
       src="https://http2.mlstatic.com/BBB-O.webp">
  <img class="ui-pdp-image ui-pdp-gallery__figure__image"
       src="https://http2.mlstatic.com/AAA-O.webp"
       data-zoom="https://http2.mlstatic.com/AAA-F.webp">
  <img class="ui-recommendations-carousel-container-img"
       src="https://http2.mlstatic.com/recommendation.webp">
</div>
"""

# The legacy layout: the full photo sits in src (note the -F).
LEGACY_LAYOUT = """
<div class="ui-pdp-gallery">
  <img class="gallery-image__image" src="https://http2.mlstatic.com/CCC-F-null.webp">
  <img class="gallery-image__image" src="https://http2.mlstatic.com/AAA-F.webp">
</div>
"""


class TestGalleryExtraction(unittest.IsolatedAsyncioTestCase):
    async def _extract(self, html: str):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content(html)
                return await page.evaluate(_GALLERY_IMAGES_JS)
            finally:
                await browser.close()

    async def test_new_layout_prefers_full_photo(self):
        """data-zoom wins over the -O.webp src; no preview next to its own full copy."""
        self.assertEqual(
            await self._extract(NEW_LAYOUT),
            [
                "https://http2.mlstatic.com/AAA-F.webp",
                "https://http2.mlstatic.com/BBB-O.webp",
            ],
        )

    async def test_legacy_layout_still_works(self):
        self.assertEqual(
            await self._extract(LEGACY_LAYOUT),
            [
                "https://http2.mlstatic.com/CCC-F-null.webp",
                "https://http2.mlstatic.com/AAA-F.webp",
            ],
        )

    async def test_both_layouts_in_one_page(self):
        """A URL appearing in both layouts is collected once."""
        page = NEW_LAYOUT + LEGACY_LAYOUT
        imgs = await self._extract(page)
        self.assertEqual(len(imgs), 3)
        self.assertEqual(imgs[0], "https://http2.mlstatic.com/AAA-F.webp")
        self.assertIn("https://http2.mlstatic.com/CCC-F-null.webp", imgs)
        self.assertEqual(len(set(imgs)), len(imgs))
        self.assertNotIn("https://http2.mlstatic.com/AAA-O.webp", imgs)
        self.assertNotIn("https://http2.mlstatic.com/recommendation.webp", imgs)

    async def test_capped_at_40(self):
        cards = "\n".join(
            f'<img class="ui-pdp-gallery__figure__image" src="https://http2.mlstatic.com/{i}-O.webp">'
            for i in range(50)
        )
        self.assertEqual(len(await self._extract(f"<div>{cards}</div>")), 40)

    async def test_empty_page(self):
        self.assertEqual(await self._extract("<div>sin imagenes</div>"), [])


class TestRedirectGuard(unittest.TestCase):
    """``_redirected_away`` must see through the ``#redirectedFromVip`` fragment."""

    LISTING = (
        "https://casa.mercadolibre.com.ar/"
        "MLA-2053057985-casa-en-venta-martinez-3-dormitorios-jardin-y-pileta-_JM"
    )

    def test_listing_page_is_not_a_redirect(self):
        self.assertFalse(_redirected_away(self.LISTING, self.LISTING))

    def test_search_page_without_fragment_is_a_redirect(self):
        self.assertTrue(
            _redirected_away(
                self.LISTING,
                "https://inmuebles.mercadolibre.com.ar/casas/venta/propiedades-individuales/"
                "bsas-gba-norte/san-isidro/martinez/",
            )
        )

    def test_redirected_from_vip_fragment_is_a_redirect(self):
        """The real shape: the id survives URL-encoded inside the fragment."""
        final = (
            "https://inmuebles.mercadolibre.com.ar/casas/venta/propiedades-individuales/"
            "bsas-gba-norte/san-isidro/martinez/"
            "#redirectedFromVip=https%3A%2F%2Fcasa.mercadolibre.com.ar%2F"
            "MLA-2053057985-casa-en-venta-martinez-3-dormitorios-jardin-y-pileta-_JM"
        )
        self.assertTrue(_redirected_away(self.LISTING, final))

    def test_other_listing_id_does_not_count(self):
        """A fragment naming a *different* MLA id is still a redirect."""
        final = (
            "https://inmuebles.mercadolibre.com.ar/casas/venta/"
            "#redirectedFromVip=https%3A%2F%2Fcasa.mercadolibre.com.ar%2FMLA-9999999999-x-_JM"
        )
        self.assertTrue(_redirected_away(self.LISTING, final))

    def test_url_without_listing_id_never_flags(self):
        self.assertFalse(_redirected_away("https://example.com/algo", "https://example.com/otra"))
        self.assertFalse(_redirected_away("", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
