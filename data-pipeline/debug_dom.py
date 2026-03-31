"""
DOM inspector: navigates to the KBLI 56102 detail page, takes a screenshot,
and dumps the full HTML so we can read the real selector structure.
"""
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

DETAIL_URL = (
    "https://oss.go.id/kbli/detail/eab92220-4cc3-4400-a0be-8e32da6f22a4"
    "?id=semua&lang=id"
)
OUT_DIR = Path("data/debug")
OUT_DIR.mkdir(parents=True, exist_ok=True)


async def main():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        print(f"Navigating to {DETAIL_URL} …")
        await page.goto(DETAIL_URL, wait_until="networkidle", timeout=60_000)

        # Extra wait for JS-rendered content
        await page.wait_for_timeout(5_000)

        # Screenshot
        shot_path = OUT_DIR / "kbli_56102_detail.png"
        await page.screenshot(path=str(shot_path), full_page=True)
        print(f"Screenshot saved → {shot_path}")

        # Full HTML dump
        html = await page.content()
        html_path = OUT_DIR / "kbli_56102_detail.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"HTML saved → {html_path}  ({len(html):,} chars)")

        # Print all elements that could be tabs (role=tab, button, [class*=tab])
        print("\n--- role=tab elements ---")
        tabs = page.locator("[role='tab']")
        count = await tabs.count()
        print(f"Found {count} role=tab element(s):")
        for i in range(count):
            txt = await tabs.nth(i).text_content()
            cls = await tabs.nth(i).get_attribute("class")
            print(f"  [{i}] text='{txt.strip()}' class='{cls}'")

        print("\n--- button elements with scale keywords ---")
        for word in ["Mikro", "Kecil", "Menengah", "Besar"]:
            btns = page.locator(f"button:has-text('{word}')")
            c = await btns.count()
            if c:
                txt = await btns.first.text_content()
                cls = await btns.first.get_attribute("class")
                print(f"  '{word}': found {c} button(s), text='{txt.strip()}', class='{cls}'")
            else:
                print(f"  '{word}': 0 buttons")

        print("\n--- headings (h1-h4) ---")
        for tag in ["h1", "h2", "h3", "h4"]:
            els = page.locator(tag)
            c = await els.count()
            for i in range(c):
                txt = (await els.nth(i).text_content() or "").strip()
                if txt:
                    print(f"  <{tag}> '{txt[:100]}'")

        print("\n--- elements containing 'URAIAN' text ---")
        uraian_els = page.locator("*:has-text('URAIAN')")
        c = await uraian_els.count()
        print(f"Found {c} element(s) containing 'URAIAN' (showing first 5):")
        for i in range(min(c, 5)):
            el = uraian_els.nth(i)
            tag = await el.evaluate("el => el.tagName")
            cls = await el.get_attribute("class") or ""
            txt = (await el.text_content() or "")[:120].replace("\n", " ")
            print(f"  [{i}] <{tag}> class='{cls}' → '{txt}'")

        print("\n--- all <li> text inside nav/tab-like containers ---")
        li_els = page.locator("ul li")
        c = await li_els.count()
        print(f"Found {c} <li> elements (showing first 20):")
        for i in range(min(c, 20)):
            txt = (await li_els.nth(i).text_content() or "").strip()[:80]
            if txt:
                print(f"  [{i}] '{txt}'")

        await browser.close()
    print("\nDone.")


asyncio.run(main())
