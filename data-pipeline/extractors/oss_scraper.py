"""
Async Playwright scraper for OSS KBLI detail pages.

Navigates the dynamic OSS portal (oss.go.id), expands the RUANG LINGKUP
accordion, clicks through the Usaha Mikro / Kecil / Menengah / Besar scale
tabs, and extracts structured regulatory data for each KBLI code.

Selectors in this file were derived from live DOM inspection of:
  https://oss.go.id/kbli/detail/eab92220-4cc3-4400-a0be-8e32da6f22a4?id=semua&lang=id

Key structural facts about the OSS portal (Next.js SPA):
  - A promotional popup overlay (z-index 9999) may block all clicks on load.
  - RUANG LINGKUP uses a Flowbite accordion (data-accordion="collapse").
  - Inside the accordion, business-scale tabs are <button role="tab"> elements
    with text "Usaha Mikro", "Usaha Kecil", etc. inside a scrollable container.
  - Tab panel: <div role="tabpanel">. Field pairs are <div class="flex items-center">
    with two <p> children: label and ": value".
  - Kewajiban data is a flex-row table inside the tab panel (No / Parameter /
    Kewenangan columns).
  - PB UMKU is a separate accordion below RUANG LINGKUP.  Each item is an
    accordion <button> whose span text is the license name.
  - Two elements share id="accordion-collapse" — select by context, not by ID.

Usage (standalone):
    python -m extractors.oss_scraper

Usage (programmatic):
    from extractors.oss_scraper import OssScraper
    import asyncio

    report = asyncio.run(OssScraper(kbli_codes=["56102", "62019"]).run())
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import chromadb
from langchain_text_splitters import RecursiveCharacterTextSplitter
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from extractors.models import KbliDetail, PbUmkuItem, ScaleScope, ScrapeReport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OSS_BASE_URL  = "https://oss.go.id"
SEARCH_URL    = OSS_BASE_URL + "/id/kbli?q={code}"
DETAIL_URL_RE = re.compile(r"/(?:id/)?kbli/detail/[0-9a-f-]{36}", re.IGNORECASE)

# RUANG LINGKUP field labels as they appear on the live page (lowercase for matching).
SCOPE_FIELD_MAP: dict[str, str] = {
    "luas lahan":         "luas_lahan",
    "tingkat risiko":     "tingkat_risiko",
    "perizinan berusaha": "perizinan_berusaha",
    "jangka waktu":       "jangka_waktu",
}

# Playwright timeouts (ms).
NAVIGATION_TIMEOUT  = 45_000   # page.goto / wait_until networkidle
SELECTOR_TIMEOUT    = 20_000   # wait_for_selector / wait_for
ACCORDION_SETTLE    = 4_000    # wait after expanding the RUANG LINGKUP accordion
TAB_SETTLE          = 2_500    # wait after clicking a scale tab

# ChromaDB / vector store settings.
CHROMA_DIR        = str(_PROJECT_ROOT / "chroma_db")
COLLECTION_NAME   = "oss_regulations"
EMBEDDING_MODEL   = "paraphrase-multilingual-MiniLM-L12-v2"
UPSERT_BATCH_SIZE = 32

# JSON output directory.
JSON_OUTPUT_DIR = _PROJECT_ROOT / "data" / "kbli_json"


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class OssScraper:
    """
    Async Playwright scraper for OSS KBLI detail pages.

    Args:
        kbli_codes:      List of KBLI codes, e.g. ["56102", "62019"].
        headless:        Run browser without a visible window.
        chroma_dir:      Persistent ChromaDB storage path.
        collection_name: ChromaDB collection to upsert into.
        save_json:       Persist each KbliDetail as a JSON file.
        vectorize:       Embed and upsert results into ChromaDB.
    """

    def __init__(
        self,
        kbli_codes: list[str],
        headless: bool = True,
        chroma_dir: str = CHROMA_DIR,
        collection_name: str = COLLECTION_NAME,
        save_json: bool = True,
        vectorize: bool = True,
    ) -> None:
        self.kbli_codes      = [str(c).strip() for c in kbli_codes]
        self.headless        = headless
        self.chroma_dir      = chroma_dir
        self.collection_name = collection_name
        self.save_json       = save_json
        self.vectorize       = vectorize

    async def run(self) -> ScrapeReport:
        """Execute the full scraping run and return a ScrapeReport."""
        report = ScrapeReport(kbli_codes_attempted=self.kbli_codes)

        async with async_playwright() as playwright:
            browser, context = await self._launch_browser(playwright)
            try:
                for index, code in enumerate(self.kbli_codes):
                    logger.info(
                        "Scraping KBLI [%d/%d]: %s",
                        index + 1, len(self.kbli_codes), code,
                    )
                    page = await context.new_page()
                    try:
                        detail = await self._scrape_kbli(page, code)
                        report.results.append(detail)
                        logger.info("KBLI %s scraped successfully.", code)
                    except Exception as exc:  # noqa: BLE001
                        report.failed_codes[code] = str(exc)
                        logger.error("KBLI %s failed: %s", code, exc, exc_info=True)
                    finally:
                        await page.close()
            finally:
                await context.close()
                await browser.close()

        logger.info(
            "Scraping complete: %d succeeded, %d failed.",
            report.success_count, report.failure_count,
        )

        if self.save_json and report.results:
            self._save_json(report)

        if self.vectorize and report.results:
            self._vectorize(report.results)

        return report

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    async def _launch_browser(
        self, playwright: Playwright
    ) -> tuple[Browser, BrowserContext]:
        """
        Prefer system Google Chrome (no download needed); fall back to
        the Playwright-managed Chromium binary.
        """
        try:
            browser = await playwright.chromium.launch(
                channel="chrome", headless=self.headless
            )
            logger.debug("Launched system Google Chrome.")
        except Exception:
            logger.warning(
                "System Chrome not found — falling back to Playwright Chromium. "
                "Run `playwright install chromium` if this also fails."
            )
            browser = await playwright.chromium.launch(headless=self.headless)

        context = await browser.new_context(
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            java_script_enabled=True,
        )
        # Block image/media/font requests — saves ~60% of bandwidth on this portal.
        await context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_()
            ),
        )
        return browser, context

    # ------------------------------------------------------------------
    # Per-KBLI orchestration
    # ------------------------------------------------------------------

    async def _scrape_kbli(self, page: Page, code: str) -> KbliDetail:
        """Full scrape flow for one KBLI code. Raises on unrecoverable failure."""
        detail_url = await self._search_and_get_detail_url(page, code)
        logger.info("KBLI %s → %s", code, detail_url)

        await self._navigate(page, detail_url)
        await self._dismiss_popup(page)

        uraian         = await self._extract_uraian(page)
        ruang_lingkup  = await self._extract_ruang_lingkup(page)

        # Kewajiban is extracted per-tab during ruang_lingkup; collect here.
        kewajiban      = await self._extract_kewajiban_from_panel(page)
        pb_umku_detail = await self._extract_pb_umku_detail(page)

        return KbliDetail(
            kbli_code=code,
            detail_url=detail_url,
            uraian=uraian,
            ruang_lingkup=ruang_lingkup,
            kewajiban_perizinan_berusaha=kewajiban,
            pb_umku_detail=pb_umku_detail,
        )

    # ------------------------------------------------------------------
    # Popup handling
    # ------------------------------------------------------------------

    async def _dismiss_popup(self, page: Page) -> None:
        """
        The OSS portal shows a promotional modal (z-index: 9999) on load
        that blocks all pointer events. Strategy:
          1. Click the modal's close button.
          2. Force-remove any remaining overlay element via JS.
        """
        try:
            close_btn = page.locator("[class*='z-[9999]'] button").first
            await close_btn.click(timeout=5_000)
            await page.wait_for_timeout(400)
            logger.debug("Popup closed via button click.")
        except Exception:
            logger.debug("No popup close button found (or already gone).")

        # Force-remove overlay regardless of React re-hydration.
        await page.evaluate("""
            document.querySelectorAll(
                '[class*="z-[9999]"], [class*="fixed"][class*="h-full"][class*="w-screen"]'
            ).forEach(el => el.remove());
        """)
        await page.wait_for_timeout(300)

    # ------------------------------------------------------------------
    # Stage 1 — Search and resolve detail URL
    # ------------------------------------------------------------------

    async def _search_and_get_detail_url(self, page: Page, code: str) -> str:
        """
        Navigate to the KBLI search page and return the UUID detail URL.

        OSS search renders <a href="/kbli/detail/{uuid}"> result cards.
        We prefer the card whose visible text contains the exact code.
        """
        await self._navigate(page, SEARCH_URL.format(code=code))

        try:
            await page.wait_for_selector(
                "a[href*='/kbli/detail/']", timeout=SELECTOR_TIMEOUT
            )
        except PlaywrightTimeoutError:
            raise RuntimeError(
                f"Search results did not render for KBLI {code}. "
                "Portal may be slow or the code does not exist."
            )

        links = await page.locator("a[href*='/kbli/detail/']").all()
        if not links:
            raise RuntimeError(f"No detail links found for KBLI {code}.")

        # Prefer exact code match in the card text.
        chosen_href: Optional[str] = None
        for link in links:
            try:
                text = (await link.text_content() or "").strip()
                href = await link.get_attribute("href") or ""
                if code in text and DETAIL_URL_RE.search(href):
                    chosen_href = href
                    break
            except Exception:  # noqa: BLE001
                continue

        if not chosen_href:
            for link in links:
                try:
                    href = await link.get_attribute("href") or ""
                    if DETAIL_URL_RE.search(href):
                        chosen_href = href
                        break
                except Exception:  # noqa: BLE001
                    continue

        if not chosen_href:
            raise RuntimeError(f"Could not resolve a detail URL for KBLI {code}.")

        return chosen_href if chosen_href.startswith("http") else OSS_BASE_URL + chosen_href

    # ------------------------------------------------------------------
    # Stage 2 — Field extraction
    # ------------------------------------------------------------------

    async def _extract_uraian(self, page: Page) -> str:
        """
        Extract the URAIAN description.

        DOM structure (confirmed from live page):
          <div class="col-span-12">
            <div class="my-2">Rumah/Warung Makan</div>   ← name
            <div>
              <div class="text-base font-semibold text-oss-gray80">URAIAN</div>
            </div>
            <div class="my-2">Kelompok ini mencakup...</div>  ← target
          </div>

        XPath: find the label div, walk up two levels, take the next my-2 sibling.
        """
        xpath = (
            "//div["
            "  normalize-space(text())='URAIAN' and "
            "  contains(@class,'text-oss-gray80')"
            "]/../following-sibling::div[contains(@class,'my-2')][1]"
        )
        try:
            el   = page.locator(f"xpath={xpath}").first
            text = await el.text_content(timeout=5_000)
            if text and text.strip():
                return text.strip()
        except Exception:  # noqa: BLE001
            pass

        logger.warning("URAIAN not extracted — returning empty string.")
        return ""

    async def _extract_ruang_lingkup(self, page: Page) -> list[ScaleScope]:
        """
        Expand the RUANG LINGKUP accordion and click through every scale tab.

        The accordion button is the FIRST [data-accordion="collapse"] button
        on the page (the second one belongs to PB UMKU).
        After expansion, scale tabs appear as <button role="tab"> inside
        a <ul role="tablist"> with a horizontal scroll container.
        """
        # Expand the RUANG LINGKUP accordion via JS (avoids popup re-intercept).
        ruang_accordion_btn = page.locator(
            "[data-accordion='collapse']"
        ).first.locator("button").first

        try:
            await ruang_accordion_btn.wait_for(state="visible", timeout=SELECTOR_TIMEOUT)
            btn_text = (await ruang_accordion_btn.text_content() or "").strip()
            logger.debug("Expanding RUANG LINGKUP accordion ('%s') via JS click.", btn_text)
            await ruang_accordion_btn.evaluate("el => el.click()")
            await page.wait_for_timeout(ACCORDION_SETTLE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not expand RUANG LINGKUP accordion: %s", exc)
            return []

        # Collect all scale tabs now visible inside the accordion panel.
        tab_buttons = page.locator("[role='tablist'] [role='tab']")
        tab_count   = await tab_buttons.count()

        if tab_count == 0:
            logger.warning("No scale tabs found after expanding accordion.")
            return []

        logger.info("Found %d scale tab(s).", tab_count)
        scopes: list[ScaleScope] = []

        for i in range(tab_count):
            tab = tab_buttons.nth(i)
            try:
                tab_name = (await tab.text_content() or "").strip()
                logger.info("  Clicking scale tab [%d/%d]: '%s'", i + 1, tab_count, tab_name)

                # JS click bypasses any residual overlay.
                await tab.evaluate("el => el.click()")
                await page.wait_for_timeout(TAB_SETTLE)

                scope = await self._extract_scope_from_panel(page, tab_name)
                scopes.append(scope)

            except Exception as exc:  # noqa: BLE001
                logger.warning("Error processing tab %d: %s", i, exc)

        return scopes

    async def _extract_scope_from_panel(self, page: Page, tab_name: str) -> ScaleScope:
        """
        Parse the active tab panel for one business scale.

        Panel DOM (confirmed):
          <div role="tabpanel">
            <div class="flex flex-wrap gap-6">
              <div class="space-y-2">
                <div class="flex items-center">
                  <p class="w-60 font-semibold ...">Luas Lahan</p>
                  <p class="text-oss-base-black">: Tidak Diatur</p>
                </div>
                ...
              </div>
            </div>
            <!-- kewajiban table below -->
          </div>

        Values start with ": " — strip that prefix.
        """
        scope_data: dict[str, Optional[str]] = {v: None for v in SCOPE_FIELD_MAP.values()}

        try:
            panel = page.locator("[role='tabpanel']").first
            await panel.wait_for(state="visible", timeout=SELECTOR_TIMEOUT)

            # Each field is a flex row with two <p> children.
            field_rows = panel.locator("div.flex.items-center:has(p.w-60)")
            row_count  = await field_rows.count()

            for i in range(row_count):
                try:
                    row   = field_rows.nth(i)
                    cells = row.locator("p")
                    if await cells.count() < 2:
                        continue

                    label = (await cells.nth(0).text_content() or "").strip().lower()
                    raw   = (await cells.nth(1).text_content() or "").strip()
                    # Values are formatted as ": text" — remove the prefix colon.
                    value = raw.lstrip(":").strip() or None

                    for key_fragment, field_name in SCOPE_FIELD_MAP.items():
                        if key_fragment in label:
                            scope_data[field_name] = value
                            break

                except Exception as exc:  # noqa: BLE001
                    logger.debug("Error reading field row %d for tab '%s': %s", i, tab_name, exc)

        except Exception as exc:  # noqa: BLE001
            logger.warning("Error extracting panel for tab '%s': %s", tab_name, exc)

        return ScaleScope(skala=tab_name, **scope_data)

    async def _extract_kewajiban_from_panel(self, page: Page) -> list[str]:
        """
        Extract Kewajiban obligations from the currently visible tab panel.

        The panel contains a flex-grid table with columns:
          No | Parameter | Kewenangan

        The Parameter column embeds a <style> block (Tailwind sanitized-content
        CSS) alongside the real text inside a <div class="sanitized-content">.
        We read from that inner div to avoid capturing the raw CSS.

        Row format stored: "Parameter — Kewenangan".
        Falls back to an empty list without raising.
        """
        items: list[str] = []
        try:
            panel = page.locator("[role='tabpanel']").first
            # Data rows have a shadow class; header row does not.
            rows  = panel.locator("div.flex.items-center.rounded-lg.bg-white")
            count = await rows.count()

            for i in range(count):
                try:
                    row  = rows.nth(i)
                    cols = row.locator("> div")  # direct children only
                    n    = await cols.count()
                    if n < 3:
                        continue

                    # Use innerText (not textContent) so that embedded <style>
                    # blocks are excluded from the returned string.
                    parameter  = (
                        await cols.nth(1).evaluate("el => el.innerText") or ""
                    ).strip()
                    kewenangan = (
                        await cols.nth(2).evaluate("el => el.innerText") or ""
                    ).strip()

                    if parameter:
                        items.append(
                            f"{parameter} — {kewenangan}" if kewenangan else parameter
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Error reading kewajiban row %d: %s", i, exc)

        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not extract kewajiban table: %s", exc)

        if not items:
            logger.warning("No kewajiban items found — returning empty list.")
        return items

    async def _extract_pb_umku_detail(self, page: Page) -> list[PbUmkuItem]:
        """
        Extract PB UMKU license names from the second accordion on the page.

        DOM structure (confirmed):
          <div class="text-base font-semibold text-oss-gray80">PB UMKU</div>
          <div data-accordion="collapse">
            <h2>
              <button ...>
                <span class="... font-semibold">Label pengawasan/...</span>
              </button>
            </h2>
          </div>

        Each accordion button span contains one PB UMKU license name.
        We collect names now; expanding each accordion for detail is optional.
        """
        items: list[PbUmkuItem] = []
        try:
            # The PB UMKU section is the second [data-accordion="collapse"] element.
            pb_accordion = page.locator("[data-accordion='collapse']").nth(1)
            btn_spans    = pb_accordion.locator("button span.font-semibold")
            count        = await btn_spans.count()

            if count == 0:
                logger.warning("No PB UMKU items found in accordion.")
                return []

            for i in range(count):
                try:
                    name = (await btn_spans.nth(i).text_content() or "").strip()
                    if name:
                        items.append(PbUmkuItem(name=name))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Error reading PB UMKU item %d: %s", i, exc)

        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not extract PB UMKU accordion: %s", exc)

        logger.info("PB UMKU items found: %d", len(items))
        return items

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def _navigate(self, page: Page, url: str) -> None:
        """
        Go to a URL, wait for network idle. Retries once on timeout.
        """
        for attempt in (1, 2):
            try:
                await page.goto(url, wait_until="networkidle", timeout=NAVIGATION_TIMEOUT)
                return
            except PlaywrightTimeoutError:
                if attempt == 1:
                    logger.warning("Navigation timeout for %s — retrying…", url)
                    await page.wait_for_timeout(3_000)
                else:
                    raise RuntimeError(f"Navigation failed after 2 attempts: {url}")
            except Exception as exc:
                raise RuntimeError(f"Navigation error for {url}: {exc}") from exc

    # ------------------------------------------------------------------
    # JSON output
    # ------------------------------------------------------------------

    def _save_json(self, report: ScrapeReport) -> None:
        JSON_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        for detail in report.results:
            path = JSON_OUTPUT_DIR / f"kbli_{detail.kbli_code}_{timestamp}.json"
            try:
                path.write_text(detail.model_dump_json(indent=2), encoding="utf-8")
                logger.info("JSON saved → %s", path)
            except OSError as exc:
                logger.error("Failed to write JSON for KBLI %s: %s", detail.kbli_code, exc)

        report_path = JSON_OUTPUT_DIR / f"report_{timestamp}.json"
        try:
            report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            logger.info("Scrape report saved → %s", report_path)
        except OSError as exc:
            logger.error("Failed to write scrape report: %s", exc)

    # ------------------------------------------------------------------
    # Vectorization
    # ------------------------------------------------------------------

    def _vectorize(self, results: list[KbliDetail]) -> dict:
        """Embed and upsert KBLI details into ChromaDB."""
        from langchain_community.embeddings import HuggingFaceEmbeddings

        logger.info("Starting vectorization of %d KBLI record(s)…", len(results))

        try:
            embeddings_model = HuggingFaceEmbeddings(
                model_name=EMBEDDING_MODEL,
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )
        except Exception as exc:
            logger.error("Failed to load embedding model: %s", exc, exc_info=True)
            return {"upserted": 0, "failed": 0}

        try:
            client     = chromadb.PersistentClient(path=self.chroma_dir)
            collection = client.get_or_create_collection(
                name=self.collection_name, metadata={"hnsw:space": "cosine"}
            )
        except Exception as exc:
            logger.error("Failed to connect to ChromaDB: %s", exc, exc_info=True)
            return {"upserted": 0, "failed": 0}

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000, chunk_overlap=100,
            separators=["\n\n", "\n", ". ", ".", " ", ""],
        )

        all_texts: list[str]  = []
        all_metas: list[dict] = []
        all_ids:   list[str]  = []

        for detail in results:
            for text, meta in detail.to_text_chunks():
                for i, chunk in enumerate(splitter.split_text(text)):
                    if not chunk.strip():
                        continue
                    fp  = f"{detail.kbli_code}::{meta.get('section')}::{i}::{chunk[:64]}"
                    cid = hashlib.sha256(fp.encode()).hexdigest()[:32]
                    all_texts.append(chunk)
                    all_metas.append({**meta, "sub_chunk_index": i})
                    all_ids.append(cid)

        upserted = failed = 0
        total    = len(all_texts)
        logger.info("Upserting %d chunk(s) in batches of %d…", total, UPSERT_BATCH_SIZE)

        for start in range(0, total, UPSERT_BATCH_SIZE):
            end   = min(start + UPSERT_BATCH_SIZE, total)
            try:
                vectors = embeddings_model.embed_documents(all_texts[start:end])
                collection.upsert(
                    ids       = all_ids[start:end],
                    embeddings= vectors,
                    documents = all_texts[start:end],
                    metadatas = all_metas[start:end],
                )
                upserted += end - start
            except Exception as exc:  # noqa: BLE001
                failed += end - start
                logger.error("Batch [%d–%d] failed: %s", start + 1, end, exc, exc_info=True)

        logger.info("Vectorization complete: %d upserted, %d failed.", upserted, failed)
        return {"upserted": upserted, "failed": failed}


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    TARGET_CODES = ["56102", "62019"]

    scraper = OssScraper(
        kbli_codes=TARGET_CODES,
        headless=True,
        save_json=True,
        vectorize=False,
    )
    report = asyncio.run(scraper.run())

    print(f"\nDone — {report.success_count} succeeded, {report.failure_count} failed.")
    for code, err in report.failed_codes.items():
        print(f"  FAILED {code}: {err}")

    sys.exit(1 if report.failure_count == len(report.kbli_codes_attempted) else 0)
