import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import settings
from scrapers.argenprop import _merge_image_urls

try:
    from playwright.async_api import async_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover
    PLAYWRIGHT_AVAILABLE = False


class TestArgenpropImageMerge(unittest.TestCase):
    def test_removes_query_variants_and_prefers_larger_image(self):
        merged = _merge_image_urls(
            ["https://img.example/1.jpg?width=320", "https://img.example/2.jpg"],
            ["https://img.example/1.jpg?width=1600", "https://img.example/3.jpg"],
        )

        self.assertEqual(merged, [
            "https://img.example/1.jpg?width=1600",
            "https://img.example/2.jpg",
            "https://img.example/3.jpg",
        ])

    def test_gallery_respects_configured_limit(self):
        source = [f"https://img.example/{index}.jpg" for index in range(settings.max_gallery_images + 2)]

        self.assertEqual(len(_merge_image_urls(source)), settings.max_gallery_images)


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "Playwright is required for DOM fixture test")
class TestArgenpropGalleryActivation(unittest.IsolatedAsyncioTestCase):
    async def test_photo_count_button_reveals_full_gallery(self):
        from scrapers.argenprop import _extract_detail_from_dom

        html = """
        <button id="photos">12 Fotos</button>
        <script>
        document.querySelector('#photos').addEventListener('click', () => {
          const gallery = document.createElement('div');
          gallery.className = 'gallery';
          gallery.innerHTML = Array.from({length: 12}, (_, i) =>
            `<img src="https://img.example/preview-${i}.jpg"
                  data-zoom="https://img.example/full-${i}.jpg">`
          ).join('');
          document.body.append(gallery);
        });
        </script>
        """
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(html)
            try:
                result = await _extract_detail_from_dom(page)
                self.assertEqual(len(result["images"]), 12)
                self.assertTrue(all("/full-" in image for image in result["images"]))
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
