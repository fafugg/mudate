"""
Diagnose image extraction for Zonaprop detail pages.

Usage (from backend/):
    python tests/test_images.py

Add URLs of houses that are missing images to URLS_PROBLEM and known-good
ones to URLS_OK.  The script prints, for each URL:
  - How many images our current extractor returns
  - The raw structure of every photo-like array found in __NEXT_DATA__
    (keys, sample values) so we can see exactly why extraction fails
  - What the DOM gallery portal contains
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from scrapers.zonaprop import _PROFILE_DIR, _extract_detail

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ── Put URLs here ─────────────────────────────────────────────────────────────
# Add the house that only returns 1 image to URLS_PROBLEM.
URLS_PROBLEM: list[str] = [
    "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-san-isidro-57830448.html",
]

URLS_OK: list[str] = [
    "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-chalet-5-dorm.-venta-martinez-santa-fe-fleming-58848455.html",
    "https://www.zonaprop.com.ar/propiedades/clasificado/veclcain-casa-en-venta-en-beccar-de-4-dorm.-entre-vias-y-55073347.html",
]

ALL_URLS = URLS_PROBLEM + URLS_OK
# ─────────────────────────────────────────────────────────────────────────────


def _is_photo(u: str) -> bool:
    return bool(u and isinstance(u, str)
                and u.startswith("http")
                and not re.search(r"\.svg(\?|$)", u, re.I))


def _extract_url(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return (item.get("url") or item.get("src") or item.get("image")
                or item.get("fullUrl") or item.get("imageUrl")
                or item.get("urlImage") or item.get("uri") or "")
    return ""


def _find_photo_arrays(obj, depth=0) -> list[list[str]]:
    """Walk obj recursively; return every array that has >1 photo URL."""
    if depth > 12 or not isinstance(obj, (dict, list)):
        return []
    if isinstance(obj, list):
        urls = [u for u in map(_extract_url, obj) if _is_photo(u)]
        found = [urls] if len(urls) > 1 else []
        for v in obj:
            found.extend(_find_photo_arrays(v, depth + 1))
        return found
    results = []
    for v in obj.values():
        results.extend(_find_photo_arrays(v, depth + 1))
    return results


def _find_all_arrays(obj, depth=0) -> list[tuple[str, list]]:
    """Walk obj recursively; return (key_path, array) for EVERY array found."""
    results = []
    if depth > 8 or not isinstance(obj, (dict, list)):
        return results
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                results.append((k, v))
            results.extend(_find_all_arrays(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_find_all_arrays(item, depth + 1))
    return results


def _summarise_array(arr: list) -> str:
    """One-line summary: length + type of first element + keys if dict."""
    if not arr:
        return "[]"
    first = arr[0]
    if isinstance(first, dict):
        keys = list(first.keys())[:8]
        sample_vals = {k: str(first[k])[:60] for k in keys}
        return f"len={len(arr)}, dict keys={keys}\n      sample={sample_vals}"
    if isinstance(first, str):
        return f"len={len(arr)}, strings, first={first[:80]}"
    return f"len={len(arr)}, type={type(first).__name__}"


async def inspect_page(page, url: str) -> None:
    label = url.split("/")[-1][:70]
    print(f"\n{'═'*72}")
    print(f"  {label}")
    print(f"{'═'*72}")
    print(f"  URL: {url}")

    # Navigate
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    for sel in ['[class*="icon-stotal"]', '[class*="publisherData-module__publisher-name"]']:
        try:
            await page.wait_for_selector(sel, timeout=8000)
        except Exception:
            pass

    # Same wait as production _scrape_detail: wait for ≥2 thumbnails with src
    try:
        await page.wait_for_function(
            """() => {
                const imgs = document.querySelectorAll(
                    '[class*="imageGrid-module__imgProperty"]'
                );
                const loaded = [...imgs].filter(img => {
                    const s = img.getAttribute('src') || '';
                    return s.startsWith('http') && !s.endsWith('.svg');
                });
                return loaded.length >= 2;
            }""",
            timeout=8000,
        )
        print("  ✓ Gallery wait: ≥2 thumbnails loaded")
    except Exception:
        print("  ✗ Gallery wait timed out — falling back to container wait")
        try:
            await page.wait_for_selector('#new-gallery-portal, #multimedia-content', timeout=4000)
        except Exception:
            pass

    # ── 1. Current extractor result (BeautifulSoup on page.content()) ─────────
    html = await page.content()
    result = _extract_detail(html)
    images = result.get("images") or []
    print(f"\n  [EXTRACTOR/BS4] images returned: {len(images)}")
    for i, u in enumerate(images[:6]):
        print(f"    [{i}] {u[:100]}")

    # ── 1b. JS extraction (same as production _scrape_detail) ─────────────────
    from scrapers.zonaprop import _dedupe_images
    js_images: list = await page.evaluate("""() => {
        function isPhoto(u) {
            return u && typeof u === 'string'
                && u.startsWith('http')
                && !/\\.svg(\\?|$)/i.test(u);
        }
        try {
            const nd = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
            function extractUrl(item) {
                if (typeof item === 'string') return item;
                if (item && typeof item === 'object')
                    return item.url || item.src || item.image || item.fullUrl
                         || item.imageUrl || item.urlImage || item.uri || '';
                return '';
            }
            function findPhotoArrays(obj, depth) {
                if (depth > 12 || obj === null || typeof obj !== 'object') return [];
                if (Array.isArray(obj)) {
                    const urls = obj.map(extractUrl).filter(isPhoto);
                    const found = urls.length > 1 ? [urls] : [];
                    return found.concat(obj.flatMap(v => findPhotoArrays(v, depth + 1)));
                }
                return Object.values(obj).flatMap(v => findPhotoArrays(v, depth + 1));
            }
            const allCandidates = findPhotoArrays(nd, 0);
            if (allCandidates.length > 0) {
                const best = allCandidates.reduce((a, b) => b.length > a.length ? b : a, []);
                if (best.length > 1) return best;
            }
        } catch(e) {}
        const root = document.getElementById('new-gallery-portal')
                  || document.getElementById('multimedia-content');
        if (!root) return [];
        const urls = new Set();
        root.querySelectorAll('img').forEach(img => {
            for (const attr of ['srcset', 'data-srcset']) {
                (img.getAttribute(attr) || '').split(',').forEach(entry => {
                    const u = entry.trim().split(/\\s+/)[0];
                    if (isPhoto(u)) urls.add(u);
                });
            }
            for (const attr of ['src', 'data-src', 'data-lazy-src', 'currentSrc']) {
                const u = img.getAttribute(attr) || img[attr] || '';
                if (isPhoto(u)) urls.add(u);
            }
        });
        root.querySelectorAll('[class*="imageGrid-module__imgProperty"]').forEach(el => {
            const style = el.getAttribute('style') || '';
            const m = style.match(/url\\(['\"]?(https?[^'\"\\)]+)['\"]?\\)/);
            if (m && isPhoto(m[1])) urls.add(m[1]);
        });
        return [...urls];
    }""")
    deduped = _dedupe_images(js_images) if js_images else []
    print(f"\n  [EXTRACTOR/JS]  images returned: {len(deduped)}")
    for i, u in enumerate(deduped[:6]):
        print(f"    [{i}] {u[:100]}")

    # ── 2. __NEXT_DATA__ photo arrays ─────────────────────────────────────────
    print("\n  [__NEXT_DATA__] scanning for photo arrays (extractUrl method)...")
    try:
        nd_text = await page.evaluate(
            "() => document.getElementById('__NEXT_DATA__')?.textContent || ''"
        )
        nd = json.loads(nd_text) if nd_text else {}

        photo_arrays = _find_photo_arrays(nd)
        if photo_arrays:
            print(f"  Found {len(photo_arrays)} candidate array(s):")
            for i, arr in enumerate(sorted(photo_arrays, key=len, reverse=True)):
                print(f"    [{i}] {len(arr)} photos — first: {arr[0][:80]}")
        else:
            print("  ✗ No photo arrays found with current extractUrl keys.")
            print("  Dumping ALL arrays in __NEXT_DATA__ to find the right one:")
            all_arrays = _find_all_arrays(nd)
            # Filter to arrays that contain at least 1 dict or string with "http"
            interesting = []
            for k, arr in all_arrays:
                if not arr:
                    continue
                first = arr[0]
                if isinstance(first, dict):
                    vals = " ".join(str(v) for v in first.values())
                    if "http" in vals:
                        interesting.append((k, arr))
                elif isinstance(first, str) and first.startswith("http"):
                    interesting.append((k, arr))
            if interesting:
                for k, arr in interesting[:15]:
                    print(f"    key={k!r:30s} {_summarise_array(arr)}")
            else:
                print("  (no interesting arrays found)")

    except Exception as e:
        print(f"  ERROR reading __NEXT_DATA__: {e}")

    # ── 3. DOM gallery portal ─────────────────────────────────────────────────
    print("\n  [DOM] gallery portal inspection...")
    dom_info = await page.evaluate("""() => {
        function isPhoto(u) {
            return u && u.startsWith('http') && !/\\.svg(\\?|$)/i.test(u);
        }
        const root = document.getElementById('new-gallery-portal')
                  || document.getElementById('multimedia-content');
        if (!root) return {found: false};

        const imgs = [...root.querySelectorAll('img')].map(img => ({
            src:         img.getAttribute('src') || '',
            dataSrc:     img.getAttribute('data-src') || '',
            srcset:      (img.getAttribute('srcset') || '').slice(0, 120),
            dataSrcset:  (img.getAttribute('data-srcset') || '').slice(0, 120),
            currentSrc:  img.currentSrc || '',
            classes:     img.className || '',
        }));

        const divs = [...root.querySelectorAll('[class*="imageGrid-module__imgProperty"]')]
            .map(el => ({
                tag:   el.tagName,
                style: (el.getAttribute('style') || '').slice(0, 120),
                src:   el.getAttribute('src') || '',
            }));

        return {found: true, imgCount: imgs.length, divCount: divs.length, imgs, divs};
    }""")

    if not dom_info.get("found"):
        print("  ✗ No gallery portal found in DOM")
    else:
        print(f"  Gallery portal: {dom_info['imgCount']} <img> tags, "
              f"{dom_info['divCount']} imageGrid divs")
        for i, img in enumerate(dom_info.get("imgs", [])[:6]):
            print(f"    img[{i}]:")
            for k, v in img.items():
                if v:
                    print(f"      {k}: {v[:100]}")
        for i, d in enumerate(dom_info.get("divs", [])[:6]):
            print(f"    div[{i}]: tag={d['tag']} style={d['style'][:80]}")


async def main():
    if not ALL_URLS:
        print("Add URLs to URLS_PROBLEM / URLS_OK at the top of this file.")
        return

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

        for url in ALL_URLS:
            try:
                await inspect_page(page, url)
            except Exception as exc:
                print(f"\n  ERROR on {url}: {exc}")

        await context.close()

    print(f"\n{'═'*72}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
