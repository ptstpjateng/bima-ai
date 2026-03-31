"""
Web scraper for DPMPTSP/OSS regulatory content.

Fetches HTML from a list of URLs and extracts clean article text using
BeautifulSoup. Designed to be fault-tolerant: a single URL failure never
aborts the rest of the run.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Tags that almost never contain the main article body.
_NOISE_TAGS = {
    "script", "style", "noscript", "header", "footer",
    "nav", "aside", "form", "button", "iframe",
}

# Common CSS class/id fragments that signal boilerplate regions.
_NOISE_PATTERNS = re.compile(
    r"(menu|navbar|sidebar|footer|cookie|advertisement|breadcrumb|social)",
    re.IGNORECASE,
)


@dataclass
class ScrapeResult:
    url: str
    text: str = ""
    success: bool = False
    error: Optional[str] = None


@dataclass
class WebScraper:
    """
    Fetches and extracts article text from a list of public URLs.

    Args:
        urls:           List of target URLs to scrape.
        timeout:        Per-request timeout in seconds (connect, read).
        delay:          Polite delay in seconds between consecutive requests.
        max_retries:    How many times to retry a request on transient errors.
        retry_backoff:  Multiplier applied to delay on each retry attempt.
        user_agent:     User-Agent header sent with every request.
    """

    urls: list[str]
    timeout: tuple[int, int] = (10, 30)
    delay: float = 1.0
    max_retries: int = 3
    retry_backoff: float = 2.0
    user_agent: str = (
        "Mozilla/5.0 (compatible; BIMA-AI-DataPipeline/1.0; "
        "+https://dpmptsp.example.go.id)"
    )

    def scrape_all(self) -> list[ScrapeResult]:
        """Run scraping over every URL; always returns one result per URL."""
        results: list[ScrapeResult] = []
        for index, url in enumerate(self.urls):
            logger.info("Scraping [%d/%d]: %s", index + 1, len(self.urls), url)
            result = self._scrape_one(url)
            results.append(result)
            if index < len(self.urls) - 1:
                time.sleep(self.delay)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _scrape_one(self, url: str) -> ScrapeResult:
        """Fetch and parse a single URL, retrying on transient failures."""
        html: Optional[str] = self._fetch_with_retries(url)
        if html is None:
            # Error already logged by _fetch_with_retries.
            return ScrapeResult(url=url, success=False, error="fetch_failed")

        try:
            text = self._extract_text(html)
        except Exception as exc:  # noqa: BLE001
            logger.error("Parse error for %s: %s", url, exc, exc_info=True)
            return ScrapeResult(url=url, success=False, error=str(exc))

        if not text:
            logger.warning("No usable text extracted from %s", url)
            return ScrapeResult(url=url, success=False, error="empty_content")

        logger.info("Extracted %d chars from %s", len(text), url)
        return ScrapeResult(url=url, text=text, success=True)

    def _fetch_with_retries(self, url: str) -> Optional[str]:
        """
        GET a URL, retrying with exponential back-off on transient errors.

        Returns the decoded response body on success, None on permanent failure.
        """
        headers = {"User-Agent": self.user_agent}
        attempt = 0
        wait = self.delay

        while attempt < self.max_retries:
            attempt += 1
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                response.raise_for_status()
                return response.text

            except requests.exceptions.Timeout:
                logger.warning(
                    "Timeout on attempt %d/%d for %s (connect=%ds, read=%ds)",
                    attempt, self.max_retries, url,
                    self.timeout[0], self.timeout[1],
                )

            except requests.exceptions.ConnectionError as exc:
                logger.warning(
                    "Connection error on attempt %d/%d for %s: %s",
                    attempt, self.max_retries, url, exc,
                )

            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                logger.error(
                    "HTTP %s for %s — not retrying.", status, url
                )
                return None  # 4xx/5xx are not transient; stop immediately.

            except requests.exceptions.TooManyRedirects:
                logger.error("Too many redirects for %s — not retrying.", url)
                return None

            except requests.exceptions.RequestException as exc:
                logger.error(
                    "Unexpected request error for %s: %s", url, exc, exc_info=True
                )
                return None

            if attempt < self.max_retries:
                logger.info("Retrying %s in %.1fs…", url, wait)
                time.sleep(wait)
                wait *= self.retry_backoff

        logger.error("All %d attempts failed for %s.", self.max_retries, url)
        return None

    def _extract_text(self, html: str) -> str:
        """
        Parse HTML and return cleaned article text.

        Strategy:
        1. Remove known noise tags outright.
        2. Prefer semantic containers (<article>, <main>, <section>).
        3. Fall back to <body>.
        4. Clean whitespace and control characters.
        """
        soup = BeautifulSoup(html, "html.parser")

        # 1. Strip noise tags (scripts, styles, nav, etc.)
        for tag in soup.find_all(_NOISE_TAGS):
            tag.decompose()

        # 2. Also strip elements whose class or id signals boilerplate.
        for tag in soup.find_all(True):
            attrs = " ".join(
                str(v)
                for key in ("class", "id")
                for v in ([tag.get(key)] if isinstance(tag.get(key), str)
                          else (tag.get(key) or []))
            )
            if _NOISE_PATTERNS.search(attrs):
                tag.decompose()

        # 3. Prefer semantic containers; fall back to <body>.
        container = (
            soup.find("article")
            or soup.find("main")
            or soup.find(id=re.compile(r"(content|main|article)", re.I))
            or soup.find(class_=re.compile(r"(content|main|article)", re.I))
            or soup.find("body")
            or soup
        )

        raw_text = container.get_text(separator="\n")
        return _clean_text(raw_text)


def _clean_text(raw: str) -> str:
    """
    Normalise extracted text:
    - Replace non-breaking spaces and common Unicode whitespace with a plain space.
    - Strip invisible/control characters (except newline and tab).
    - Collapse runs of spaces/tabs on each line.
    - Remove completely blank lines.
    - Collapse consecutive blank lines to a single blank line.
    """
    # Replace non-breaking and other exotic spaces.
    text = raw.replace("\xa0", " ").replace("\u200b", "").replace("\u200c", "")

    # Strip control characters except \n and \t.
    text = re.sub(r"[^\S\n\t ]+", " ", text)

    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        # Collapse inner whitespace runs.
        line = re.sub(r"[ \t]{2,}", " ", line)
        lines.append(line)

    # Remove runs of more than one consecutive empty line.
    cleaned: list[str] = []
    prev_blank = False
    for line in lines:
        is_blank = line == ""
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank

    return "\n".join(cleaned).strip()
