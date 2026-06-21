"""
Quick smoke-test for the Zonaprop detail extractor.

Usage (from backend/):
    python tests/test_detail.py

Requires: playwright (playwright install chromium), beautifulsoup4, lxml
"""
import asyncio
import os
import sys

# Make sure local scrapers package is importable when run directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapers.zonaprop import _scrape_detail, _PROFILE_DIR

from playwright.async_api import async_playwright

URLS = [
    "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-chalet-5-dorm.-venta-martinez-santa-fe-fleming-58848455.html",
    "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-beccar-de-4-dorm.-entre-vias-y-55073347.html",
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def _fmt_val(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, list):
        if not v:
            return "[]"
        joined = " | ".join(str(x) for x in v[:5])
        suffix = f" … (+{len(v)-5} more)" if len(v) > 5 else ""
        return joined + suffix
    if isinstance(v, str) and len(v) > 120:
        return v[:117] + "..."
    return str(v)


def _print_table(title: str, data: dict) -> None:
    print(f"\n{'═'*72}")
    print(f"  {title}")
    print(f"{'═'*72}")
    if not data:
        print("  (no data extracted)")
        return
    key_w = max(len(k) for k in data) + 2
    for k, v in data.items():
        print(f"  {k:<{key_w}} {_fmt_val(v)}")


async def fetch_and_extract(url: str, page) -> dict:
    """Delegates to the real _scrape_detail so waits are identical to production."""
    print(f"\n→ Loading: {url[-80:]}")
    result = await _scrape_detail(page, url)
    return result


async def main():
    os.makedirs(_PROFILE_DIR, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=_PROFILE_DIR,
            headless=False,
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="es-AR",
            args=["--disable-blink-features=AutomationControlled"],
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = context.pages[0] if context.pages else await context.new_page()

        results = []
        for url in URLS:
            try:
                data = await fetch_and_extract(url, page)
                results.append((url, data))
            except Exception as exc:
                print(f"  ERROR: {exc}")
                results.append((url, {}))

        await context.close()

    for url, data in results:
        short = url.split("/")[-1][:70]
        _print_table(short, data)

    print(f"\n{'═'*72}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
