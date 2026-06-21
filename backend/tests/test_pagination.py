"""
Tests click-based pagination — clicks the page 2 button from within page 1
instead of navigating directly to /pagina-2.html.
Usage: .venv/bin/python tests/test_pagination.py "/casas-ph-venta-san-isidro-300000-310001-dolar.html"
"""
import asyncio, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE_URL = "https://www.zonaprop.com.ar"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
PROFILE = os.path.expanduser("~/.casa_tracker_browser")


async def main(search_filter):
    from playwright.async_api import async_playwright
    from scrapers.zonaprop import _extract_cards_js

    os.makedirs(PROFILE, exist_ok=True)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=PROFILE, headless=False,
            user_agent=UA, viewport={"width": 1280, "height": 900}, locale="es-AR",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await ctx.new_page()

        # ── Page 1 via URL ───────────────────────────────────────────────
        print(f"Page 1 (URL): {BASE_URL}{search_filter}")
        resp = await page.goto(f"{BASE_URL}{search_filter}", wait_until="domcontentloaded", timeout=30000)
        print(f"  HTTP {resp.status}  url={page.url[:80]}")
        await asyncio.sleep(2)

        # Accept cookie consent
        try:
            btn = await page.query_selector('button:has-text("Acepto")')
            if btn:
                await btn.click()
                print("  Accepted cookies")
                await asyncio.sleep(1)
        except Exception:
            pass

        cards1 = await _extract_cards_js(page)
        print(f"  Cards: {len(cards1)}")
        if not cards1:
            print("  !! No cards on page 1 — stopping")
            await ctx.close()
            return

        # ── Scroll to bottom so pagination renders ───────────────────────
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)

        # ── Find pagination links ────────────────────────────────────────
        pag_info = await page.evaluate("""() => {
            const links = [...document.querySelectorAll('a[href]')]
                .filter(a => /-pagina-\\d+/.test(a.href))
                .map(a => ({ text: a.textContent.trim(), href: a.href, cls: a.className.slice(0,80) }));
            const btns = [...document.querySelectorAll('button')]
                .filter(b => /siguiente|next|>/i.test(b.textContent))
                .map(b => ({ text: b.textContent.trim(), cls: b.className.slice(0,80) }));
            return { links, btns };
        }""")
        print(f"\n  Pagination <a> links ({len(pag_info['links'])}):")
        for l in pag_info['links'][:8]:
            print(f"    text={l['text']!r}  href={l['href'][:80]}")
        print(f"  Pagination <button>s ({len(pag_info['btns'])}):")
        for b in pag_info['btns'][:4]:
            print(f"    text={b['text']!r}")

        # ── Click page 2 ─────────────────────────────────────────────────
        # Use the Zonaprop-specific pattern: pagination URLs always end in
        # "-pagina-2.html" (with a leading dash), which never appears in card hrefs.
        next_el = await page.query_selector('a[href*="-pagina-2"]')
        if not next_el:
            # Fallback: aria-label "Siguiente" on the next-arrow link
            next_el = await page.query_selector('a[aria-label*="iguiente"], a[title*="iguiente"]')
        if not next_el:
            print("\n  No page 2 / next button found.")
            await ctx.close()
            return

        href = await next_el.get_attribute("href")
        print(f"\nPage 2 (click): clicking href={href}")
        await next_el.scroll_into_view_if_needed()
        await asyncio.sleep(0.5)

        await next_el.click()
        # SPA: no full page reload — wait for the URL to change via history.pushState
        for _ in range(30):
            await asyncio.sleep(0.5)
            if "pagina-2" in page.url:
                break
        await asyncio.sleep(2)

        print(f"  Final URL: {page.url}")
        cards2 = await _extract_cards_js(page)
        print(f"  Cards: {len(cards2)}")
        if cards2:
            ids1 = {c.get("id") for c in cards1}
            ids2 = {c.get("id") for c in cards2}
            overlap = ids1 & ids2
            print(f"  IDs page1={list(ids1)[:3]}...")
            print(f"  IDs page2={list(ids2)[:3]}...")
            print(f"  Overlap (featured dupes): {len(overlap)}")
            print("  ✓ Click-based pagination WORKS" if ids1 != ids2 else "  !! Same IDs as page 1")
        else:
            print("  !! No cards on page 2")

        await ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: .venv/bin/python tests/test_pagination.py "/casas-ph-venta-san-isidro-300000-310001-dolar.html"')
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
