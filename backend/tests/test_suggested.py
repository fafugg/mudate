"""
Test: identify "Encontramos otras propiedades que podrían interesarte" section
on a Zonaprop search page and confirm which cards belong to it.

Usage (from backend/):
    .venv/bin/python tests/test_suggested.py

Hypothesis:
  - All cards use [data-posting-type] (our current extractor already relies on this)
  - The "suggested" section is introduced by an <h2> with class matching
    thinPostingsList-module__h2-style
  - Cards in the suggested section live inside a div matching thin-postings-container
  - Cards BEFORE that div (direct children of postingsList-module__postings-container)
    are the real results
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scrapers.zonaprop import _PROFILE_DIR

from playwright.async_api import async_playwright

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# A URL that is known to show the "suggested" section (last/only page with few results)
TEST_URL = "https://www.zonaprop.com.ar/casas-ph-venta-san-isidro-martinez-acassuso-beccar-con-pileta-y-jardin-mas-de-3-habitaciones-300000-300001-dolar.html"


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

        print(f"\n→ Loading: {TEST_URL}")
        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)

        try:
            await page.wait_for_selector("[data-posting-type]", timeout=10000)
        except Exception:
            print("✗ No [data-posting-type] cards found — possible bot block")
            await context.close()
            return

        import asyncio as _asyncio
        await _asyncio.sleep(2)

        result = await page.evaluate("""() => {
            // ── 1. Locate the suggested-section heading ──────────────────────
            const h2Els = [...document.querySelectorAll('h2')].filter(
                h => /otras propiedades.*interesar/i.test(h.textContent)
            );
            const heading = h2Els[0] || null;

            // ── 2. Locate the thin-postings-container ─────────────────────────
            const thinContainer = document.querySelector('[class*="thin-postings-container"]');

            // ── 3. All cards ──────────────────────────────────────────────────
            const allCards = [...document.querySelectorAll('[data-posting-type]')];

            // ── 4. Classify each card ─────────────────────────────────────────
            function isSuggested(card) {
                // Walk up the DOM — if any ancestor matches thin-postings-container,
                // this card is in the suggested section.
                let el = card.parentElement;
                while (el) {
                    if (el.className && typeof el.className === 'string' &&
                        el.className.includes('thin-postings-container')) return true;
                    el = el.parentElement;
                }
                return false;
            }

            const real      = allCards.filter(c => !isSuggested(c));
            const suggested = allCards.filter(c =>  isSuggested(c));

            // ── 5. Check data-id on each group ────────────────────────────────
            const realIds      = real.map(c => c.getAttribute('data-id') || '?');
            const suggestedIds = suggested.map(c => c.getAttribute('data-id') || '?');

            // ── 6. Check for other possible containers after the heading ──────
            // Are there cards that are NOT in thin-postings-container but appear
            // after the heading in DOM order?
            let cardsAfterHeadingOutsideThin = [];
            if (heading) {
                allCards.forEach(card => {
                    if (isSuggested(card)) return; // already classified
                    // Compare DOM position: card comes after heading?
                    const pos = heading.compareDocumentPosition(card);
                    // DOCUMENT_POSITION_FOLLOWING = 4
                    if (pos & 4) cardsAfterHeadingOutsideThin.push(card.getAttribute('data-id') || '?');
                });
            }

            // ── 7. Heading info ───────────────────────────────────────────────
            const headingInfo = heading ? {
                found: true,
                text: heading.textContent.trim().slice(0, 80),
                className: heading.className,
                parentClass: heading.parentElement?.className || '',
            } : { found: false };

            // ── 8. Thin container info ────────────────────────────────────────
            const thinInfo = thinContainer ? {
                found: true,
                className: thinContainer.className,
                tagName: thinContainer.tagName,
                cardCount: thinContainer.querySelectorAll('[data-posting-type]').length,
            } : { found: false };

            // ── 9. Are ALL suggested cards inside thinContainer only? ─────────
            // Check if there's a second thin-postings-container or other wrappers
            const allThinContainers = [...document.querySelectorAll('[class*="thin-postings-container"]')];

            return {
                totalCards: allCards.length,
                realCount: real.length,
                suggestedCount: suggested.length,
                realIds,
                suggestedIds,
                cardsAfterHeadingOutsideThin,
                heading: headingInfo,
                thinContainer: thinInfo,
                thinContainerCount: allThinContainers.length,
                thinContainerClasses: allThinContainers.map(el => el.className),
            };
        }""")

        print(f"\n{'═'*72}")
        print(f"  RESULTS")
        print(f"{'═'*72}")
        print(f"  Total [data-posting-type] cards : {result['totalCards']}")
        print(f"  Real cards                       : {result['realCount']}")
        print(f"  Suggested cards                  : {result['suggestedCount']}")

        print(f"\n  Heading 'otras propiedades':")
        h = result['heading']
        if h['found']:
            print(f"    ✓ Found")
            print(f"    text      : {h['text']}")
            print(f"    className : {h['className'][:80]}")
            print(f"    parentClass: {h['parentClass'][:80]}")
        else:
            print(f"    ✗ NOT found")

        print(f"\n  thin-postings-container divs: {result['thinContainerCount']}")
        t = result['thinContainer']
        if t['found']:
            print(f"    ✓ Found")
            print(f"    className : {t['className'][:80]}")
            print(f"    tagName   : {t['tagName']}")
            print(f"    cards inside: {t['cardCount']}")
        else:
            print(f"    ✗ NOT found")

        print(f"\n  thin-postings-container class names:")
        for cls in result['thinContainerClasses']:
            print(f"    · {cls[:100]}")

        if result['cardsAfterHeadingOutsideThin']:
            print(f"\n  ⚠ Cards AFTER heading but OUTSIDE thin-container: {result['cardsAfterHeadingOutsideThin']}")
        else:
            print(f"\n  ✓ No cards after heading outside thin-container")

        print(f"\n  Real IDs      : {result['realIds']}")
        print(f"  Suggested IDs : {result['suggestedIds']}")

        # ── Verdict ───────────────────────────────────────────────────────────
        print(f"\n{'═'*72}")
        if result['heading']['found'] and result['thinContainer']['found']:
            if result['suggestedCount'] == result['thinContainer']['cardCount']:
                print("  ✓ HYPOTHESIS CONFIRMED")
                print("    All suggested cards are inside thin-postings-container.")
                print("    Filter: skip cards whose ancestor matches 'thin-postings-container'.")
            else:
                print("  ⚠ PARTIAL — thin-container card count doesn't match classified suggested count")
        elif not result['heading']['found']:
            print("  ✗ Heading not found — page may not have suggested section")
        elif not result['thinContainer']['found']:
            print("  ✗ thin-postings-container not found — structure may be different")
        print(f"{'═'*72}\n")

        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
