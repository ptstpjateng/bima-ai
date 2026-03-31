"""
Debug: click the RUANG LINGKUP accordion, dump expanded HTML, and check
what scale-specific URL parameters look like.
"""
import asyncio
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

DETAIL_URL = (
    "https://oss.go.id/kbli/detail/eab92220-4cc3-4400-a0be-8e32da6f22a4"
    "?id=semua&lang=id"
)
OUT_DIR = Path("data/debug")
OUT_DIR.mkdir(parents=True, exist_ok=True)

SCALE_IDS = ["semua", "mikro", "kecil", "menengah", "besar"]


async def main():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            locale="id-ID", timezone_id="Asia/Jakarta",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # ── 1. Expand the "Seluruh" accordion and capture results ──────────
        print("Loading detail page …")
        await page.goto(DETAIL_URL, wait_until="networkidle", timeout=60_000)
        await page.wait_for_timeout(3_000)

        # ── Dismiss any popup/modal overlay before interacting ────────────
        # The OSS portal sometimes shows a promotional popup with z-index 9999.
        # Strategy: press Escape, then try clicking a close/X button.
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
        for close_selector in [
            "[class*='z-[9999]'] button",
            "[class*='modal'] button",
            "button[aria-label*='close' i]",
            "button[aria-label*='tutup' i]",
            ".fixed button",
        ]:
            try:
                close_btns = page.locator(close_selector)
                if await close_btns.count() > 0:
                    await close_btns.first.click(timeout=3_000)
                    await page.wait_for_timeout(500)
                    print(f"Dismissed popup via: {close_selector}")
                    break
            except Exception:
                continue

        # Force-remove the popup overlay from the DOM via JS so it can't
        # intercept pointer events regardless of React re-renders.
        await page.evaluate("""
            document.querySelectorAll(
                '[class*="z-[9999]"], [class*="fixed"][class*="h-full"]'
            ).forEach(el => el.remove());
        """)
        await page.wait_for_timeout(500)
        print("Popup overlay removed via JS.")

        # Click the accordion button via JS (bypasses any remaining overlay).
        btn = page.locator("[data-accordion='collapse'] button").first
        btn_text = await btn.text_content()
        print(f"Accordion button: '{btn_text.strip()}'")
        await btn.evaluate("el => el.click()")
        await page.wait_for_timeout(4_000)   # wait for expansion animation

        html_after = await page.content()
        (OUT_DIR / "kbli_56102_expanded.html").write_text(html_after, encoding="utf-8")
        await page.screenshot(path=str(OUT_DIR / "kbli_56102_expanded.png"), full_page=True)
        print("Saved expanded HTML and screenshot.")

        # Parse and print what appeared inside the accordion panel
        soup = BeautifulSoup(html_after, "html.parser")
        accordion = soup.find("div", {"data-accordion": "collapse"})
        print("\n=== ACCORDION CONTENT AFTER CLICK ===")
        print(accordion.prettify()[:5000] if accordion else "(not found)")

        # ── 2. Try each scale-specific URL and see what changes ────────────
        base_url = "https://oss.go.id/kbli/detail/eab92220-4cc3-4400-a0be-8e32da6f22a4"
        print("\n\n=== CHECKING SCALE-SPECIFIC URL PARAMS ===")
        for scale_id in SCALE_IDS:
            url = f"{base_url}?id={scale_id}&lang=id"
            await page.goto(url, wait_until="networkidle", timeout=60_000)
            await page.wait_for_timeout(2_000)
            h = await page.content()
            soup2 = BeautifulSoup(h, "html.parser")
            acc = soup2.find("div", {"data-accordion": "collapse"})
            if acc:
                btn_spans = acc.find_all("span", class_=lambda c: c and "font-semibold" in c)
                labels = [s.get_text(strip=True) for s in btn_spans if s.get_text(strip=True)]
                print(f"  ?id={scale_id:10s} → accordion buttons: {labels}")
            else:
                print(f"  ?id={scale_id:10s} → no accordion found")

        await browser.close()
    print("\nDone.")


asyncio.run(main())
