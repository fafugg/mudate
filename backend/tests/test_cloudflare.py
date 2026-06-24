"""
Diagnose Cloudflare / bot-detection issues on Zonaprop.

Usage (from backend/):
    .venv/bin/python tests/test_cloudflare.py

What it checks:
  1. _HEADLESS flag value and why
  2. Browser profile directory — exists? size?
  3. HTTP response status
  4. Whether we land on a Cloudflare challenge page
  5. Whether [data-posting-type] cards are found
  6. Page title + URL after navigation
  7. Cookies set by Cloudflare (cf_clearance, __cf_bm, etc.)

If the profile is poisoned (headless Docker run stored bot-flagged cookies),
the fix is: rm -rf ~/.mudate_browser
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapers.zonaprop import _PROFILE_DIR, _HEADLESS, UA, BASE_URL

TEST_URL = f"{BASE_URL}/casas-ph-venta-san-isidro-300000-310001-dolar.html"

SEP = "═" * 72


async def main():
    from playwright.async_api import async_playwright

    print(f"\n{SEP}")
    print("  ENVIRONMENT")
    print(SEP)
    print(f"  _HEADLESS      : {_HEADLESS}")
    print(f"  /.dockerenv    : {os.path.exists('/.dockerenv')}")
    print(f"  $DISPLAY       : {os.environ.get('DISPLAY', '(not set)')}")
    print(f"  PLAYWRIGHT_HEADLESS: {os.environ.get('PLAYWRIGHT_HEADLESS', '(not set)')}")

    print(f"\n{SEP}")
    print("  BROWSER PROFILE")
    print(SEP)
    if os.path.isdir(_PROFILE_DIR):
        size_mb = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, files in os.walk(_PROFILE_DIR)
            for f in files
        ) / 1_048_576
        print(f"  Path  : {_PROFILE_DIR}")
        print(f"  Size  : {size_mb:.1f} MB")
        # Look for Cloudflare cookie files
        cf_files = []
        for dp, _, files in os.walk(_PROFILE_DIR):
            for f in files:
                if "cookie" in f.lower() or "storage" in f.lower():
                    cf_files.append(os.path.relpath(os.path.join(dp, f), _PROFILE_DIR))
        print(f"  Cookie/storage files: {len(cf_files)}")
    else:
        print(f"  {_PROFILE_DIR} does not exist — fresh profile will be created")

    print(f"\n{SEP}")
    print("  NAVIGATION TEST")
    print(SEP)
    print(f"  URL: {TEST_URL}")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=_PROFILE_DIR,
            headless=_HEADLESS,
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale="es-AR",
            args=["--disable-blink-features=AutomationControlled"],
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Navigate
        resp = await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        status   = resp.status if resp else "N/A"
        final_url = page.url
        title     = await page.title()

        print(f"  HTTP status : {status}")
        print(f"  Final URL   : {final_url}")
        print(f"  Page title  : {title}")

        # Detect Cloudflare challenge (English and Spanish variants)
        is_cf_challenge = (status == 403) or await page.evaluate("""() => {
            const body = document.body?.innerText || '';
            const title = document.title || '';
            return (
                /challenge|just a moment|checking your browser|cloudflare|un momento/i.test(title) ||
                /challenge|just a moment|checking your browser|verificar.*humano/i.test(body.slice(0, 600)) ||
                !!document.querySelector('#cf-challenge-running, #challenge-form, .cf-error-code')
            );
        }""")
        print(f"\n  Cloudflare challenge detected: {'⚠ YES' if is_cf_challenge else '✓ NO'}")

        # Count cards
        try:
            await page.wait_for_selector("[data-posting-type]", timeout=8000)
            card_count = await page.evaluate(
                "() => document.querySelectorAll('[data-posting-type]').length"
            )
        except Exception:
            card_count = 0
        print(f"  [data-posting-type] cards    : {card_count}")

        # Cloudflare cookies
        cookies = await context.cookies()
        cf_cookies = [c for c in cookies if any(
            k in c["name"] for k in ["cf_", "__cf", "cloudflare"]
        )]
        print(f"\n  Cloudflare cookies ({len(cf_cookies)}):")
        for c in cf_cookies:
            expires = c.get("expires", -1)
            print(f"    {c['name']:<30} expires={expires:.0f}  domain={c.get('domain','')}")
        if not cf_cookies:
            print("    (none)")

        # Body snippet
        body_text = await page.evaluate("() => document.body?.innerText?.slice(0, 300) || ''")
        print(f"\n  Body snippet:\n    {body_text[:300].replace(chr(10), ' ')}")

        await context.close()

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  VERDICT")
    print(SEP)
    if is_cf_challenge:
        print("  ✗ CLOUDFLARE CHALLENGE — browser is being blocked.")
        print()
        print("  Most likely cause: the Docker/headless run poisoned the browser")
        print("  profile with bot-flagged cookies. Fix:")
        print(f"    rm -rf {_PROFILE_DIR}")
        print("  Then run the scraper locally once to rebuild a clean profile.")
    elif card_count == 0:
        print("  ✗ NO CARDS — page loaded but no listings found.")
        print("  Check the body snippet above for clues.")
    else:
        print(f"  ✓ OK — {card_count} cards found, no Cloudflare block.")
    print(f"{SEP}\n")


if __name__ == "__main__":
    asyncio.run(main())
