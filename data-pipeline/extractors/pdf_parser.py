"""
PDF parser for local regulatory documents.

Iterates through every PDF in data/pdfs/, extracts text page-by-page using
pdfplumber, and returns clean structured results. A corrupted file or a
failed page never aborts the rest of the run.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pdfplumber

logger = logging.getLogger(__name__)

_PDF_DIR = Path(__file__).resolve().parents[1] / "data" / "pdfs"


@dataclass
class PageResult:
    page_number: int          # 1-based
    text: str = ""
    success: bool = False
    error: Optional[str] = None


@dataclass
class DocumentResult:
    path: Path
    pages: list[PageResult] = field(default_factory=list)
    success: bool = False
    error: Optional[str] = None

    @property
    def full_text(self) -> str:
        """Concatenate all successfully extracted page texts."""
        return "\n\n".join(p.text for p in self.pages if p.success and p.text)

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    @property
    def successful_pages(self) -> int:
        return sum(1 for p in self.pages if p.success)


class PDFParser:
    """
    Recursively parses all PDF files found under a directory.

    Args:
        pdf_dir:            Directory to search. Defaults to data/pdfs/.
        extract_tables:     When True, table cells are included in the text
                            output (converted to plain rows). Expensive on
                            complex PDFs; disable if not needed.
        min_page_chars:     Pages whose cleaned text is shorter than this
                            threshold are treated as effectively empty and
                            flagged with a warning (not an error).
    """

    def __init__(
        self,
        pdf_dir: Path = _PDF_DIR,
        extract_tables: bool = False,
        min_page_chars: int = 10,
    ) -> None:
        self.pdf_dir = Path(pdf_dir)
        self.extract_tables = extract_tables
        self.min_page_chars = min_page_chars

    def parse_all(self) -> list[DocumentResult]:
        """
        Discover and parse every PDF in self.pdf_dir (recursive).
        Always returns one DocumentResult per file found.
        """
        if not self.pdf_dir.exists():
            logger.error(
                "PDF directory does not exist: %s", self.pdf_dir
            )
            return []

        pdf_files = sorted(self.pdf_dir.rglob("*.pdf"))
        if not pdf_files:
            logger.warning("No PDF files found in %s", self.pdf_dir)
            return []

        logger.info("Found %d PDF file(s) in %s", len(pdf_files), self.pdf_dir)
        results: list[DocumentResult] = []
        for index, path in enumerate(pdf_files):
            logger.info(
                "Parsing [%d/%d]: %s", index + 1, len(pdf_files), path.name
            )
            results.append(self._parse_file(path))
        return results

    def parse_file(self, path: Path) -> DocumentResult:
        """Parse a single PDF file. Public entry point for one-off calls."""
        path = Path(path)
        if not path.is_file():
            logger.error("File not found: %s", path)
            return DocumentResult(path=path, success=False, error="file_not_found")
        return self._parse_file(path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_file(self, path: Path) -> DocumentResult:
        result = DocumentResult(path=path)
        try:
            with pdfplumber.open(path) as pdf:
                if not pdf.pages:
                    logger.warning("PDF has no pages: %s", path.name)
                    result.error = "no_pages"
                    return result

                for page in pdf.pages:
                    page_result = self._parse_page(page, path)
                    result.pages.append(page_result)

        except pdfplumber.utils.exceptions.PDFSyntaxError as exc:
            logger.error(
                "Corrupted or invalid PDF syntax in %s: %s", path.name, exc
            )
            result.error = f"pdf_syntax_error: {exc}"
            return result

        except PermissionError:
            logger.error("Permission denied reading %s", path)
            result.error = "permission_denied"
            return result

        except OSError as exc:
            logger.error("OS error reading %s: %s", path.name, exc)
            result.error = f"os_error: {exc}"
            return result

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Unexpected error opening %s: %s", path.name, exc, exc_info=True
            )
            result.error = f"unexpected: {exc}"
            return result

        result.success = result.successful_pages > 0
        logger.info(
            "Parsed %s — %d/%d pages succeeded.",
            path.name, result.successful_pages, result.total_pages,
        )
        return result

    def _parse_page(self, page, path: Path) -> PageResult:
        """Extract and clean text from one pdfplumber page object."""
        # pdfplumber pages are 1-based by convention; page.page_number is 0-based.
        page_num = page.page_number + 1
        try:
            raw_text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""

            if self.extract_tables:
                raw_text = self._merge_tables(page, raw_text)

            text = _clean_text(raw_text)

            if len(text) < self.min_page_chars:
                logger.debug(
                    "%s p.%d: effectively empty (%d chars) — likely scanned image.",
                    path.name, page_num, len(text),
                )
                return PageResult(
                    page_number=page_num,
                    text=text,
                    success=True,  # Not an error; just sparse content.
                )

            return PageResult(page_number=page_num, text=text, success=True)

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Error extracting page %d from %s: %s",
                page_num, path.name, exc, exc_info=True,
            )
            return PageResult(
                page_number=page_num,
                success=False,
                error=str(exc),
            )

    def _merge_tables(self, page, base_text: str) -> str:
        """
        Extract tables from the page and append them as plain text rows.
        Failures here are non-fatal; we fall back to base_text only.
        """
        try:
            tables = page.extract_tables()
            if not tables:
                return base_text
            table_lines: list[str] = []
            for table in tables:
                for row in table:
                    cells = [str(cell).strip() if cell is not None else "" for cell in row]
                    table_lines.append(" | ".join(cells))
            return base_text + "\n" + "\n".join(table_lines)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Table extraction failed on a page: %s", exc)
            return base_text


def _clean_text(raw: str) -> str:
    """
    Normalise text extracted from a PDF page:
    - Substitute non-breaking spaces and soft hyphens.
    - Strip invisible Unicode control characters (keep newline, tab, space).
    - Collapse whitespace runs within lines.
    - Remove fully blank lines.
    - Collapse multiple consecutive blank lines to one.
    """
    if not raw:
        return ""

    # Common PDF artefacts.
    text = (
        raw.replace("\xa0", " ")    # non-breaking space
           .replace("\xad", "")     # soft hyphen
           .replace("\u200b", "")   # zero-width space
           .replace("\u200c", "")   # zero-width non-joiner
           .replace("\ufffd", "")   # replacement character
    )

    # Remove control characters except \n and \t.
    text = re.sub(r"[^\S\n\t ]+", " ", text)

    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r"[ \t]{2,}", " ", line)
        lines.append(line)

    # Collapse consecutive blank lines.
    cleaned: list[str] = []
    prev_blank = False
    for line in lines:
        is_blank = line == ""
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank

    return "\n".join(cleaned).strip()
