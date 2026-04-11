"""
BIMA-AI — Multi-Agent PDF Parsing Pipeline  (Deterministic + AI Hybrid)
========================================================================

Processes government OSS PDFs (KBLI Detail + PB-UMKU Detail) through a
3-agent pipeline and vectorizes the merged output into ChromaDB.

Agents
------
  Agent 1 (Extractor)   — PDF → Pandas DataFrame using pdfplumber only.
                          NO LLM CALL. Pure deterministic table extraction.
  Agent 2 (Structurer)  — DataFrames → validated JSON (KbliDetailEnriched).
                          Pandas handles: cleaning, forward-fill of merged cells,
                          and left-join merge on Nomenklatur/Nama Izin.
                          LLM ONLY used per-record to map the condensed dict to
                          our exact Pydantic schema (tiny prompt, ~500–1500 tokens
                          vs 50,000+ tokens in the old all-markdown approach).
                          Self-healing: retry loop with error feedback.
  Agent 3 (Ingestor)    — JSON → ChromaDB embeddings + PostgreSQL status update

Usage
-----
    python pdf_agent_pipeline.py --job-id 1

Environment variables (from data-pipeline/.env):
    GEMINI_API_KEY        Google AI Studio key
    GEMINI_MODEL          e.g. models/gemini-2.5-flash
    LARAVEL_BACKEND_URL   e.g. http://backend:80
    LARAVEL_API_KEY       must match backend INTERNAL_API_KEY
    CHROMA_DB_PATH        e.g. /app/chroma_db
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import httpx
import pandas as pd
import pdfplumber
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(Path(__file__).parent / "data" / "pdf_pipeline.log")),
    ],
)
logger = logging.getLogger("pdf_agent")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL       = os.environ.get("GEMINI_MODEL", "models/gemini-2.5-flash")
BACKEND_URL        = os.environ.get("LARAVEL_BACKEND_URL", "http://backend:80")
API_KEY            = os.environ.get("LARAVEL_API_KEY", "")
CHROMA_DB_PATH     = os.environ.get("CHROMA_DB_PATH", "/app/chroma_db")
BACKEND_STORAGE    = Path("/app/backend_storage")   # mounted from backend container
EMBEDDING_MODEL    = "paraphrase-multilingual-MiniLM-L12-v2"
CHROMA_COLLECTION  = "oss_regulations"

HEADERS            = {"X-Internal-Key": API_KEY, "Accept": "application/json"}
GEMINI_URL         = (
    f"https://generativelanguage.googleapis.com/v1beta/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

# ---------------------------------------------------------------------------
# Pydantic schemas (strict contract between agents)
# ---------------------------------------------------------------------------


class ScaleScope(BaseModel):
    skala: str
    tingkat_risiko: Optional[str] = None
    perizinan_berusaha: Optional[str] = None
    jangka_waktu: Optional[str] = None
    luas_lahan: Optional[str] = None
    pb_umku: Optional[str] = None


class PbUmkuDetail(BaseModel):
    """Full detail for one PB-UMKU type — extracted from PB-UMKU Detail PDF."""
    nama_izin: str
    persyaratan: list[str] = Field(default_factory=list)
    jangka_waktu: Optional[str] = None
    kewajiban: list[str] = Field(default_factory=list)
    parameter_pemenuhan: Optional[str] = None
    kewenangan: Optional[str] = None
    waktu_pemenuhan: Optional[str] = None
    biaya: Optional[str] = None
    sanksi: Optional[str] = None


class KbliExtracted(BaseModel):
    """KBLI data extracted from KBLI Detail PDF (before PB-UMKU enrichment)."""
    kbli_code: str
    kbli_description: str = ""
    uraian: str = ""
    ruang_lingkup: list[ScaleScope] = Field(default_factory=list)
    pb_umku_names: list[str] = Field(default_factory=list)


class KbliDetailEnriched(BaseModel):
    """Fully merged record: KBLI details + PB-UMKU details injected."""
    kbli_code: str
    kbli_description: str = ""
    uraian: str = ""
    ruang_lingkup: list[ScaleScope] = Field(default_factory=list)
    pb_umku_names: list[str] = Field(default_factory=list)
    pb_umku_detail: list[PbUmkuDetail] = Field(default_factory=list)
    source: str = "pdf"
    extracted_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class KbliListResponse(BaseModel):
    kbli_list: list[KbliExtracted]


class PbUmkuListResponse(BaseModel):
    pb_umku_list: list[PbUmkuDetail]


# ---------------------------------------------------------------------------
# Gemini API helper (with exponential backoff)
# ---------------------------------------------------------------------------


async def _call_gemini(prompt: str, max_retries: int = 4) -> str:
    """POST to Gemini REST API with exponential backoff on 429/503."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 8192, "temperature": 0.1},
    }
    delay = 2.0
    async with httpx.AsyncClient(timeout=180) as client:
        for attempt in range(max_retries):
            try:
                resp = await client.post(GEMINI_URL, json=payload)
                if resp.status_code in (429, 503):
                    wait = delay * (2 ** attempt)
                    logger.warning(
                        "Gemini %d on attempt %d — waiting %.0fs",
                        resp.status_code, attempt + 1, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    raise ValueError("Gemini returned no candidates")
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if "text" in p)
                return text.strip()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (429, 503) or attempt == max_retries - 1:
                    raise
                await asyncio.sleep(delay * (2 ** attempt))
    raise RuntimeError(f"Gemini failed after {max_retries} attempts")


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` fences that Gemini sometimes wraps output in."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:] if lines[0].startswith("```") else lines
        lines = lines[:-1] if lines and lines[-1].strip() == "```" else lines
        text = "\n".join(lines).strip()
    return text


async def _structured_gemini_call(
    prompt: str,
    response_model: type[BaseModel],
    max_retries: int = 3,
) -> BaseModel:
    """
    Call Gemini and validate response against a Pydantic schema.
    Self-healing: on validation failure, include the error in the next prompt.
    """
    last_error: Optional[str] = None
    base_prompt = prompt

    for attempt in range(max_retries):
        try:
            if attempt > 0 and last_error:
                healing_prompt = (
                    f"{base_prompt}\n\n"
                    f"⚠️ PERBAIKAN DIPERLUKAN: Respons sebelumnya gagal validasi dengan error:\n"
                    f"{last_error}\n\n"
                    f"Kembalikan JSON yang valid sesuai schema yang diminta. "
                    f"Jangan tambahkan markdown fence atau teks lain di luar JSON."
                )
            else:
                healing_prompt = base_prompt

            raw = await _call_gemini(healing_prompt)
            clean = _strip_markdown_fences(raw)
            data = json.loads(clean)
            return response_model.model_validate(data)

        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)[:500]
            logger.warning("Attempt %d/%d failed validation: %s", attempt + 1, max_retries, last_error)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 * (attempt + 1))

    raise ValueError(f"Structured extraction failed after {max_retries} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Backend API helpers
# ---------------------------------------------------------------------------


async def _report_progress(job_id: int, pct: int) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.patch(
                f"{BACKEND_URL}/api/internal/pdf-jobs/{job_id}/progress",
                json={"pct": pct},
                headers=HEADERS,
            )
    except Exception as exc:
        logger.warning("Failed to report progress %d%% for job %d: %s", pct, job_id, exc)


async def _report_complete(
    job_id: int,
    chunks: int,
    kbli_processed: int,
    result_json: str,
) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{BACKEND_URL}/api/internal/pdf-jobs/{job_id}/complete",
            json={
                "chunks_created": chunks,
                "kbli_processed": kbli_processed,
                "result_json": result_json,
            },
            headers=HEADERS,
        )


async def _report_fail(job_id: int, error: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{BACKEND_URL}/api/internal/pdf-jobs/{job_id}/fail",
                json={"error": error[:2000]},
                headers=HEADERS,
            )
    except Exception as exc:
        logger.error("Failed to report failure for job %d: %s", job_id, exc)


# ---------------------------------------------------------------------------
# Legacy helpers — kept for test_agents.py compatibility
# ---------------------------------------------------------------------------


def _extract_pdf_text(pdf_path: Path) -> list[dict]:
    """
    Extract text and table content from each PDF page using pdfplumber.
    Returns a list of page dicts: { page_num, text, table_text, combined, char_count }

    NOTE: This is the legacy per-page text extractor used by test_agents.py.
    The main pipeline now uses extract_tables_with_plumber() instead.
    """
    pages = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        logger.info("Extracting %d pages from %s", total, pdf_path.name)
        for i, page in enumerate(pdf.pages):
            raw_text = page.extract_text() or ""

            table_md = ""
            try:
                tables = page.extract_tables() or []
                for table in tables:
                    if not table:
                        continue
                    rows = []
                    for row in table:
                        cleaned = [str(cell or "").replace("\n", " ").strip() for cell in row]
                        rows.append(" | ".join(cleaned))
                    table_md += "\n".join(rows) + "\n\n"
            except Exception as exc:
                logger.debug("Table extraction error on page %d: %s", i + 1, exc)

            combined = f"=== Halaman {i + 1} ===\n{raw_text}\n"
            if table_md.strip():
                combined += f"\n[TABEL]:\n{table_md}"

            pages.append({
                "page_num": i + 1,
                "text": raw_text,
                "table_text": table_md,
                "combined": combined,
                "char_count": len(raw_text) + len(table_md),
            })
    return pages


def _fuzzy_match(name: str, candidates: list[str], threshold: float = 0.7) -> Optional[str]:
    """
    Find the best fuzzy match for `name` in `candidates`.
    Returns the best match if similarity >= threshold, else None.
    """
    best_match = None
    best_score = 0.0
    name_lower = name.lower().strip()

    for candidate in candidates:
        score = SequenceMatcher(None, name_lower, candidate.lower().strip()).ratio()
        if score > best_score:
            best_score = score
            best_match = candidate

    return best_match if best_score >= threshold else None


def _match_and_merge(
    kbli_list: list[KbliExtracted],
    pb_umku_map: dict[str, PbUmkuDetail],
) -> list[KbliDetailEnriched]:
    """
    Legacy merge helper — kept for test_agents.py compatibility.
    The main pipeline now uses the pandas-based structurer_agent instead.
    """
    enriched: list[KbliDetailEnriched] = []
    candidate_names = list(pb_umku_map.keys())

    for kbli in kbli_list:
        matched: list[PbUmkuDetail] = []

        for name in kbli.pb_umku_names:
            if name in pb_umku_map:
                matched.append(pb_umku_map[name])
            else:
                fuzzy = _fuzzy_match(name, candidate_names, threshold=0.75)
                if fuzzy:
                    matched.append(pb_umku_map[fuzzy])

        enriched.append(KbliDetailEnriched(
            kbli_code=kbli.kbli_code,
            kbli_description=kbli.kbli_description,
            uraian=kbli.uraian,
            ruang_lingkup=kbli.ruang_lingkup,
            pb_umku_names=kbli.pb_umku_names,
            pb_umku_detail=matched,
        ))

    return enriched


# ---------------------------------------------------------------------------
# Agent 1 — Deterministic Extractor (PDF → Pandas DataFrame, NO LLM)
# ---------------------------------------------------------------------------


def extract_tables_with_plumber(pdf_path: Path) -> pd.DataFrame:
    """
    Deterministic PDF table extraction using pdfplumber.

    Strategy (tried in order):
      1. Border-based  — page.extract_tables() with default settings.
         Works for PDFs with drawn cell borders.
      2. Text-strategy — page.extract_tables(table_settings={...}) using
         word-alignment to infer column/row boundaries.
         Works for text-layout PDFs (no visible borders).
      3. Words fallback — page.extract_words() with x-position clustering.
         Last resort for scanned/OCR PDFs where neither table strategy fires.

    Across all strategies:
      - First non-empty table's first row becomes the canonical header.
      - Rows that repeat the header are skipped (table continued on next page).
      - All rows are padded / trimmed to the canonical column count.
      - Returns one master DataFrame.
    """
    TEXT_SETTINGS = {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "min_words_vertical": 3,
        "min_words_horizontal": 1,
    }

    # Keywords that indicate a legitimate table header row
    HEADER_KEYWORDS = {
        "kode", "kbli", "judul", "skala", "risiko", "izin", "perizinan",
        "persyaratan", "nomenklatur", "tingkat", "usaha", "nomor", "nama",
        "jangka", "waktu", "kewajiban", "sanksi", "biaya",
    }

    def _looks_like_header(row: list[str]) -> bool:
        """True if the row has ≥ 2 distinct cells matching known header keywords."""
        hits = sum(
            1 for cell in row
            if any(kw in cell.lower() for kw in HEADER_KEYWORDS)
        )
        return hits >= 2

    all_rows: list[list[str]] = []
    detected_header: Optional[list[str]] = None
    pages_with_tables = 0

    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        logger.info("[pdfplumber] Scanning %d pages: %s", total, pdf_path.name)

        for page_num, page in enumerate(pdf.pages, 1):
            # ── Strategy 1: border-based ──────────────────────────────────
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []

            # ── Strategy 2: text-strategy (only for early pages / header search)
            # Text-strategy is accurate but slow (~300-500ms/page).
            # Use it only until we find the real header, then switch to words-based.
            if not tables and detected_header is None and page_num <= 10:
                try:
                    tables = page.extract_tables(table_settings=TEXT_SETTINGS) or []
                except Exception:
                    tables = []

            # ── Strategy 3: words-based (fast, ~50-100ms/page) ───────────
            # Primary fallback for all pages once header is established,
            # or for any page where border + text-strategy both failed.
            if not tables:
                try:
                    tables = _words_to_table(page)
                except Exception:
                    pass

            if not tables:
                continue

            pages_with_tables += 1
            for table in tables:
                if not table:
                    continue

                # Clean every cell: collapse newlines, strip whitespace
                cleaned_table = [
                    [str(cell or "").replace("\n", " ").strip() for cell in row]
                    for row in table
                    if any(cell for cell in row)
                ]
                if len(cleaned_table) < 2:
                    continue

                if detected_header is None:
                    # Search all rows of this table for the real header
                    header_idx = None
                    for idx, row in enumerate(cleaned_table):
                        if _looks_like_header(row):
                            header_idx = idx
                            break

                    if header_idx is not None:
                        detected_header = cleaned_table[header_idx]
                        all_rows.extend(cleaned_table[header_idx + 1:])
                        logger.info(
                            "[pdfplumber] Real header on page %d row %d (%d cols): %s …",
                            page_num, header_idx, len(detected_header),
                            detected_header[:5],
                        )
                    else:
                        # No header found yet — skip this table, keep looking
                        logger.debug(
                            "[pdfplumber] Page %d: no header-like row found, skipping",
                            page_num,
                        )
                        continue
                else:
                    first_row = cleaned_table[0]
                    ncols = len(detected_header)
                    rows_to_add = (
                        cleaned_table[1:]
                        if _looks_like_header(first_row)
                        else cleaned_table
                    )
                    for row in rows_to_add:
                        padded = row[:ncols] + [""] * (ncols - len(row))
                        if any(c for c in padded):
                            all_rows.append(padded)

    logger.info(
        "[pdfplumber] Pages with tables: %d/%d | total data rows: %d",
        pages_with_tables, total, len(all_rows),
    )

    if not all_rows or detected_header is None:
        logger.warning("[pdfplumber] No table data extracted from %s", pdf_path.name)
        return pd.DataFrame()

    ncols = len(detected_header)
    padded_all = [
        row[:ncols] + [""] * (ncols - len(row))
        for row in all_rows
    ]

    df = pd.DataFrame(padded_all, columns=detected_header)
    logger.info(
        "[pdfplumber] Master DataFrame: %d rows × %d cols from %s",
        len(df), len(df.columns), pdf_path.name,
    )
    return df


def _words_to_table(page) -> list[list[list[str]]]:
    """
    Fallback table builder using pdfplumber word positions.
    Groups words by y-coordinate (rows) then x-coordinate clusters (columns).
    Used for scanned PDFs where neither border nor text strategy finds tables.
    Returns a list containing one table (list of rows).
    """
    words = page.extract_words(x_tolerance=5, y_tolerance=3) or []
    if len(words) < 5:
        return []

    # Collect all distinct x0 positions as candidate column anchors
    x_positions = sorted(set(round(w["x0"] / 8) * 8 for w in words))
    if not x_positions:
        return []

    # Cluster x positions (within 20px = same column)
    col_anchors: list[float] = [x_positions[0]]
    for x in x_positions[1:]:
        if x - col_anchors[-1] > 20:
            col_anchors.append(x)

    # Group words by row (y0 within 5px tolerance)
    rows_dict: dict[int, list] = {}
    for w in words:
        row_key = round(w["top"] / 5) * 5
        rows_dict.setdefault(row_key, []).append(w)

    table: list[list[str]] = []
    for _, row_words in sorted(rows_dict.items()):
        row_cells = [""] * len(col_anchors)
        for w in sorted(row_words, key=lambda x: x["x0"]):
            # Find which column this word belongs to
            col_idx = 0
            for i, anchor in enumerate(col_anchors):
                if w["x0"] >= anchor - 10:
                    col_idx = i
            row_cells[col_idx] = (row_cells[col_idx] + " " + w["text"]).strip()
        if any(c for c in row_cells):
            table.append(row_cells)

    return [table] if len(table) > 1 else []


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Clean a raw pdfplumber DataFrame:
      - Replace empty strings with NaN for proper pandas handling.
      - Drop rows where ALL cells are NaN (blank separator rows).
      - Forward-fill the first 4 columns (handles PDF merged/spanned cells).
      - Replace remaining NaN with empty string.
    """
    if df.empty:
        return df

    df = df.copy()
    df = df.replace("", pd.NA)
    df = df.dropna(how="all").reset_index(drop=True)

    # Forward-fill left-side columns that represent merged cells in the PDF
    for col in df.columns[:4]:
        df[col] = df[col].ffill()

    df = df.fillna("")
    return df


def _find_column(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    """
    Return the df column whose name best fuzzy-matches any of the candidate
    key strings (case-insensitive, ignoring spaces/underscores).
    Returns None if no column scores ≥ 0.65.
    """
    best_col: Optional[str] = None
    best_score = 0.0

    for col in df.columns:
        col_norm = col.lower().replace(" ", "").replace("_", "").replace("/", "")
        for cand in candidates:
            cand_norm = cand.lower().replace(" ", "").replace("_", "").replace("/", "")
            score = SequenceMatcher(None, col_norm, cand_norm).ratio()
            if score > best_score:
                best_score = score
                best_col = col

    return best_col if best_score >= 0.65 else None


def _find_kbli_code_column(df: pd.DataFrame) -> Optional[int]:
    """
    Find the column INDEX (not name) that contains 5-digit KBLI codes.
    Tries column name matching first, then falls back to content scanning.
    Returns the column index (positional), or None if not found.
    """
    # Content-based detection: find column with most 5-digit entries
    best_idx: Optional[int] = None
    best_count = 0

    for i in range(len(df.columns)):
        try:
            count = df.iloc[:, i].astype(str).str.match(r"^\d{5}$").sum()
            if count > best_count:
                best_count = count
                best_idx = i
        except Exception:
            continue

    return best_idx if best_count >= 1 else None


def _has_valid_kbli_codes(df: pd.DataFrame) -> bool:
    """Return True if the DataFrame contains at least one 5-digit KBLI code in any column."""
    # Iterate by position to avoid issues with duplicate/empty column names
    for i in range(len(df.columns)):
        try:
            col_series = df.iloc[:, i].astype(str)
            if col_series.str.match(r"^\d{5}$").any():
                return True
        except Exception:
            continue
    return False


def extract_text_based_kbli(pdf_path: Path) -> pd.DataFrame:
    """
    Fallback extractor for scanned/OCR PDFs where pdfplumber cannot detect
    proper table structures.

    Strategy:
      1. Extract raw text from every page with pdfplumber.
      2. Scan line by line for 5-digit KBLI codes.
      3. Accumulate the surrounding context (up to 30 lines) per code.
      4. Return a DataFrame with columns: [kbli_code, raw_text].

    The raw_text field is kept small (~500-1500 chars) so the LLM call in
    Agent 2 stays cheap.
    """
    import re
    KBLI_RE = re.compile(r"^\s*(\d{5})\b")
    CONTEXT_LINES = 25

    code_context: dict[str, list[str]] = {}  # code → list of lines

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

            for i, line in enumerate(lines):
                m = KBLI_RE.match(line)
                if m:
                    code = m.group(1)
                    # Take the matched line + next CONTEXT_LINES lines as context
                    context_lines = lines[i: i + CONTEXT_LINES]
                    if code not in code_context:
                        code_context[code] = context_lines
                    else:
                        # Append extra context from later pages
                        code_context[code].extend(context_lines)

    if not code_context:
        logger.warning("[text-extract] No 5-digit KBLI codes found in %s", pdf_path.name)
        return pd.DataFrame()

    rows = [
        {"kbli_code": code, "raw_text": " | ".join(ctx)[:2000]}
        for code, ctx in sorted(code_context.items())
    ]
    df = pd.DataFrame(rows)
    logger.info(
        "[text-extract] Found %d unique KBLI codes in %s",
        len(df), pdf_path.name,
    )
    return df


def extract_text_based_pbumku(pdf_path: Path) -> pd.DataFrame:
    """
    Fallback extractor for PB-UMKU PDFs using text regex scanning.

    Looks for permit name patterns (e.g., lines starting with 'Sertifikat', 'Izin', 'Tanda
    Daftar', 'TDUP', 'TDKB') and accumulates context.
    Returns DataFrame with columns: [nama_izin, raw_text].
    """
    import re
    # Common PB-UMKU name patterns in Indonesian government docs
    PERMIT_RE = re.compile(
        r"^\s*((?:Sertifikat|Izin|Tanda\s+Daftar|TDUP|TDKB|Nomor\s+Induk)[^,\n]{5,80})",
        re.IGNORECASE,
    )
    CONTEXT_LINES = 30

    permit_context: dict[str, list[str]] = {}

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

            for i, line in enumerate(lines):
                m = PERMIT_RE.match(line)
                if m:
                    name = m.group(1).strip()
                    ctx = lines[i: i + CONTEXT_LINES]
                    if name not in permit_context:
                        permit_context[name] = ctx
                    else:
                        permit_context[name].extend(ctx)

    if not permit_context:
        logger.warning("[text-extract] No PB-UMKU permit names found in %s", pdf_path.name)
        return pd.DataFrame()

    rows = [
        {"nama_izin": name, "raw_text": " | ".join(ctx)[:2000]}
        for name, ctx in permit_context.items()
    ]
    df = pd.DataFrame(rows)
    logger.info(
        "[text-extract] Found %d PB-UMKU permits in %s",
        len(df), pdf_path.name,
    )
    return df


def extractor_agent(pdf_path: Path, doc_type: str) -> pd.DataFrame:
    """
    Agent 1: Deterministic PDF → DataFrame extraction. No LLM, no network.

    For PDFs with proper table structure (digital PDFs with borders):
      → Uses extract_tables_with_plumber() — returns full columnar DataFrame.

    Fallback for scanned/OCR PDFs without vector table borders:
      → kbli: extract_text_based_kbli() — returns [kbli_code, raw_text] DataFrame.
      → pb_umku: extract_text_based_pbumku() — returns [nama_izin, raw_text] DataFrame.

    Args:
        pdf_path: Absolute path to the PDF file.
        doc_type: 'kbli' | 'pb_umku' — drives fallback extraction strategy.

    Returns:
        DataFrame ready for Agent 2 structuring.
    """
    start = time.time()
    logger.info("[Agent 1] Extracting %s PDF: %s", doc_type.upper(), pdf_path.name)

    # ── Primary: table-based extraction ──────────────────────────────────────
    df = extract_tables_with_plumber(pdf_path)

    if not df.empty and _has_valid_kbli_codes(df):
        elapsed = time.time() - start
        logger.info(
            "[Agent 1] Table extraction OK: %d rows × %d cols in %.1fs",
            len(df), len(df.columns), elapsed,
        )
        return df

    # ── Fallback: text-regex extraction ──────────────────────────────────────
    logger.info(
        "[Agent 1] Table extraction yielded no valid KBLI codes — "
        "switching to text-regex fallback for %s PDF",
        doc_type.upper(),
    )

    if doc_type == "kbli":
        df = extract_text_based_kbli(pdf_path)
    else:
        df = extract_text_based_pbumku(pdf_path)

    elapsed = time.time() - start
    if df.empty:
        logger.warning("[Agent 1] Text-regex fallback also found nothing in %s", pdf_path.name)
    else:
        logger.info(
            "[Agent 1] Text-regex fallback: %d entries in %.1fs (NO LLM used)",
            len(df), elapsed,
        )

    return df


# ---------------------------------------------------------------------------
# Agent 2 — Pandas Merge + LLM Structuring (one small LLM call per KBLI)
# ---------------------------------------------------------------------------


async def _llm_structure_condensed(condensed: dict) -> KbliDetailEnriched:
    """
    The LLM's ONLY job in the new pipeline:
    Map a small (~500–1500 token) condensed dict to our exact Pydantic schema.

    The dict contains raw row data with whatever column names came from the PDF.
    The LLM handles the final semantic mapping and type coercion.
    """
    prompt = (
        "Kamu adalah validator data KBLI OSS Indonesia yang presisi.\n"
        "Berikut adalah data mentah SATU entri KBLI yang diekstrak dari tabel PDF pemerintah "
        "(Lampiran PP No. 5/2021 atau turunannya). Nama kolom bisa tidak standar.\n\n"
        "DATA MENTAH:\n"
        f"{json.dumps(condensed, ensure_ascii=False, indent=2)}\n\n"
        "TUGASMU: Petakan data di atas ke schema JSON berikut secara tepat.\n"
        "Gunakan penilaian terbaikmu untuk mencocokkan nama kolom dengan field schema.\n\n"
        "SCHEMA YANG DIHARAPKAN:\n"
        "{\n"
        '  "kbli_code": "56102",\n'
        '  "kbli_description": "Nama KBLI",\n'
        '  "uraian": "Teks uraian lengkap",\n'
        '  "ruang_lingkup": [\n'
        '    {\n'
        '      "skala": "Mikro",\n'
        '      "tingkat_risiko": "Rendah",\n'
        '      "perizinan_berusaha": "NIB",\n'
        '      "jangka_waktu": null,\n'
        '      "luas_lahan": null,\n'
        '      "pb_umku": "Nama PB-UMKU atau null"\n'
        '    }\n'
        "  ],\n"
        '  "pb_umku_names": ["Nama Izin 1", "Nama Izin 2"],\n'
        '  "pb_umku_detail": [\n'
        '    {\n'
        '      "nama_izin": "Nama Izin 1",\n'
        '      "persyaratan": ["syarat 1", "syarat 2"],\n'
        '      "jangka_waktu": "30 hari",\n'
        '      "kewajiban": ["kewajiban 1"],\n'
        '      "parameter_pemenuhan": null,\n'
        '      "kewenangan": null,\n'
        '      "waktu_pemenuhan": null,\n'
        '      "biaya": null,\n'
        '      "sanksi": null\n'
        '    }\n'
        "  ]\n"
        "}\n\n"
        "ATURAN:\n"
        "- Kembalikan HANYA JSON, tanpa markdown fence atau teks lain\n"
        "- Gunakan null (bukan string kosong) untuk field yang tidak tersedia\n"
        "- pb_umku_detail diisi dari data di kunci 'pbumku_rows' jika ada\n"
        "- Setiap baris di 'scale_rows' adalah satu elemen ruang_lingkup\n"
        "- pb_umku_names adalah daftar unik semua nama izin PB-UMKU yang disebut\n"
        "- Jika kbli_code tidak bisa diidentifikasi, gunakan nilai dari 'kbli_code_detected'\n"
    )
    return await _structured_gemini_call(prompt, KbliDetailEnriched)


async def structurer_agent(
    kbli_df: pd.DataFrame,
    pbumku_df: pd.DataFrame,
    kbli_filter: Optional[list[str]],
) -> list[KbliDetailEnriched]:
    """
    Agent 2: Deterministic pandas merge + minimal LLM structuring.

    Pipeline:
      1. Clean both DataFrames with pandas (drop empties, forward-fill merged cells).
      2. Auto-detect merge key columns using fuzzy column-name matching.
      3. Left-join KBLI df with PB-UMKU df on the Nomenklatur / Nama Izin column.
      4. Group by KBLI code.
      5. For each KBLI group → create a condensed dict → one small LLM call.

    Token savings vs old approach:
      Old: ~50,000+ chars of markdown per PDF chunk per LLM call
      New: ~500–1,500 chars condensed dict per KBLI per LLM call
    """
    logger.info("[Agent 2] Starting Pandas-based Structurer agent")

    # ── Step 1: Clean DataFrames ──────────────────────────────────────────────
    kbli_df  = clean_dataframe(kbli_df)
    pbumku_df = clean_dataframe(pbumku_df)

    logger.info("[Agent 2] KBLI   DF: %d rows × %d cols | cols=%s",
                len(kbli_df), len(kbli_df.columns), list(kbli_df.columns))
    logger.info("[Agent 2] PB-UMKU DF: %d rows × %d cols | cols=%s",
                len(pbumku_df), len(pbumku_df.columns), list(pbumku_df.columns))

    # ── Step 2: Detect key columns ────────────────────────────────────────────
    # Try name-based matching first, then content-based positional fallback
    kbli_code_col    = _find_column(kbli_df,  ["kode", "kbli", "kodekbli", "no", "nomor"])
    kbli_name_col    = _find_column(kbli_df,  ["nama", "uraian", "namakbli", "judul", "deskripsi"])
    kbli_pbumku_col  = _find_column(kbli_df,  ["pbumku", "nomenklatur", "namaizin",
                                                "perizinanberusaha", "jenisizin"])
    pbumku_name_col  = _find_column(pbumku_df, ["namaizin", "nomenklatur", "pbumku",
                                                 "nama", "jenisizin"])

    # Content-based KBLI code column detection (handles OCR-shifted columns)
    kbli_code_idx = _find_kbli_code_column(kbli_df)
    if kbli_code_idx is not None:
        kbli_code_col_positional = kbli_df.columns[kbli_code_idx]
        if kbli_code_col != kbli_code_col_positional:
            logger.info(
                "[Agent 2] Name-based kbli_code_col=%r overridden by content-based col[%d]=%r",
                kbli_code_col, kbli_code_idx, kbli_code_col_positional,
            )
            kbli_code_col = kbli_code_col_positional

    logger.info(
        "[Agent 2] Column map — kbli_code: %r (idx=%s) | kbli_name: %r | "
        "kbli→pb_umku: %r | pb_umku name: %r",
        kbli_code_col, kbli_code_idx, kbli_name_col, kbli_pbumku_col, pbumku_name_col,
    )

    # ── Step 3: Apply KBLI filter ─────────────────────────────────────────────
    if kbli_filter and kbli_code_col:
        kbli_df = kbli_df[kbli_df[kbli_code_col].isin(kbli_filter)].reset_index(drop=True)
        logger.info("[Agent 2] After KBLI filter: %d rows", len(kbli_df))

    # ── Step 4: Merge KBLI with PB-UMKU ──────────────────────────────────────
    if kbli_pbumku_col and pbumku_name_col and not pbumku_df.empty:
        # Rename PB-UMKU name column to avoid collision
        pbumku_renamed = pbumku_df.rename(columns={pbumku_name_col: "_pb_name"})
        merged_df = pd.merge(
            kbli_df,
            pbumku_renamed,
            left_on=kbli_pbumku_col,
            right_on="_pb_name",
            how="left",
            suffixes=("", "_pbumku"),
        )
        logger.info(
            "[Agent 2] Merged DataFrame: %d rows × %d cols",
            len(merged_df), len(merged_df.columns),
        )
    else:
        logger.warning(
            "[Agent 2] Could not identify merge columns — using KBLI data only "
            "(PB-UMKU details will be empty)"
        )
        merged_df = kbli_df.copy()

    # Fill any NaN introduced by the left-join
    merged_df = merged_df.fillna("")

    # ── Detect if DataFrames are text-based fallback (have 'raw_text' column) ──
    kbli_is_text_based  = "raw_text" in kbli_df.columns
    pbumku_is_text_based = "raw_text" in pbumku_df.columns

    if kbli_is_text_based:
        logger.info("[Agent 2] KBLI DF is text-based (raw_text mode) — skipping pandas merge")
        return await _structure_text_based(kbli_df, pbumku_df)

    # ── Step 5: Group by KBLI code → condensed dict → LLM structuring ─────────
    group_col = kbli_code_col or merged_df.columns[0]
    logger.info("[Agent 2] Grouping by column: %r", group_col)

    # PB-UMKU columns are those that came from the right-side of the merge
    pbumku_cols = [c for c in merged_df.columns if c.endswith("_pbumku") or c == "_pb_name"]
    kbli_only_cols = [c for c in merged_df.columns if c not in pbumku_cols]

    groups = merged_df.groupby(group_col)
    total_groups = len(groups)
    enriched_records: list[KbliDetailEnriched] = []

    logger.info("[Agent 2] Processing %d KBLI groups through LLM structurer", total_groups)

    for i, (kbli_code_val, group_df) in enumerate(groups):
        kbli_code_str = str(kbli_code_val).strip()
        if not kbli_code_str or kbli_code_str.lower() in ("nan", "", "kode", "no"):
            continue

        # Build condensed dict with all available data
        scale_rows = group_df[kbli_only_cols].to_dict(orient="records")

        pbumku_rows: list[dict] = []
        if pbumku_cols:
            pb_seen: set[str] = set()
            for _, row in group_df.iterrows():
                pb_row = {c: row[c] for c in pbumku_cols if row[c]}
                pb_key = str(row.get("_pb_name", "")).strip()
                if pb_key and pb_key not in pb_seen:
                    pb_seen.add(pb_key)
                    pbumku_rows.append(pb_row)

        condensed = {
            "kbli_code_detected": kbli_code_str,
            "scale_rows": scale_rows,
            "pbumku_rows": pbumku_rows,
        }

        try:
            record = await _llm_structure_condensed(condensed)
            if not record.kbli_code:
                record.kbli_code = kbli_code_str
            enriched_records.append(record)
            logger.info(
                "[Agent 2] ✓ KBLI %-8s (%d/%d) | scopes=%d | pb_umku=%d",
                kbli_code_str, i + 1, total_groups,
                len(record.ruang_lingkup), len(record.pb_umku_detail),
            )
        except Exception as exc:
            logger.error("[Agent 2] ✗ KBLI %s failed LLM structuring: %s", kbli_code_str, exc)

        await asyncio.sleep(0.5)

    logger.info(
        "[Agent 2] Structuring complete: %d/%d records successful",
        len(enriched_records), total_groups,
    )
    return enriched_records


async def _structure_text_based(
    kbli_df: pd.DataFrame,
    pbumku_df: pd.DataFrame,
) -> list[KbliDetailEnriched]:
    """
    Structure text-based fallback DataFrames ([kbli_code, raw_text]).

    For each KBLI code, find the matching PB-UMKU text (if any) and send a
    compact condensed dict to the LLM. Token cost: ~500-2000 per KBLI.
    """
    # Build PB-UMKU lookup from raw_text if available
    pbumku_lookup: dict[str, str] = {}
    if not pbumku_df.empty and "raw_text" in pbumku_df.columns:
        name_col = "nama_izin" if "nama_izin" in pbumku_df.columns else pbumku_df.columns[0]
        for _, row in pbumku_df.iterrows():
            pbumku_lookup[str(row[name_col])] = str(row.get("raw_text", ""))

    enriched_records: list[KbliDetailEnriched] = []
    total = len(kbli_df)
    logger.info("[Agent 2/text] Structuring %d KBLI text entries via LLM", total)

    for i, row in kbli_df.iterrows():
        kbli_code = str(row.get("kbli_code", "")).strip()
        raw_text  = str(row.get("raw_text", "")).strip()

        if not kbli_code:
            continue

        # Find related PB-UMKU text (fuzzy search on permit names mentioned in kbli raw_text)
        related_pbumku: list[dict] = []
        for permit_name, permit_text in pbumku_lookup.items():
            if permit_name[:20].lower() in raw_text.lower():
                related_pbumku.append({
                    "nama_izin": permit_name,
                    "raw_text": permit_text[:500],
                })

        condensed = {
            "kbli_code_detected": kbli_code,
            "kbli_raw_text": raw_text[:1500],   # capped to keep prompt small
            "pbumku_raw": related_pbumku[:3],   # top 3 matched permits
        }

        try:
            record = await _llm_structure_condensed(condensed)
            if not record.kbli_code:
                record.kbli_code = kbli_code
            enriched_records.append(record)
            logger.info(
                "[Agent 2/text] ✓ KBLI %-8s (%d/%d)",
                kbli_code, i + 1, total,
            )
        except Exception as exc:
            logger.error("[Agent 2/text] ✗ KBLI %s: %s", kbli_code, exc)

        await asyncio.sleep(0.5)

    logger.info(
        "[Agent 2/text] Complete: %d/%d records",
        len(enriched_records), total,
    )
    return enriched_records


# ---------------------------------------------------------------------------
# Agent 3 — Ingestor (JSON → ChromaDB + PostgreSQL) — UNCHANGED
# ---------------------------------------------------------------------------


def _generate_chunks(record: KbliDetailEnriched) -> list[tuple[str, dict, str]]:
    """
    Generate (text, metadata, doc_id) tuples for ChromaDB ingestion.
    Each semantic unit gets its own chunk to maximise retrieval precision.
    """
    chunks: list[tuple[str, dict, str]] = []
    base_meta = {
        "kbli_code": record.kbli_code,
        "source": "pdf",
        "source_url": "uploaded_pdf",
    }
    safe_code = record.kbli_code.replace("/", "_")

    # 1. Uraian chunk
    if record.uraian:
        chunks.append((
            f"KBLI {record.kbli_code} ({record.kbli_description}) – Uraian:\n{record.uraian}",
            {**base_meta, "section": "uraian"},
            f"pdf_{safe_code}_uraian",
        ))

    # 2. Per-scale ruang lingkup chunks
    for i, scope in enumerate(record.ruang_lingkup):
        lines = [f"KBLI {record.kbli_code} – Ruang Lingkup Skala {scope.skala}:"]
        if scope.tingkat_risiko:
            lines.append(f"Tingkat Risiko: {scope.tingkat_risiko}")
        if scope.perizinan_berusaha:
            lines.append(f"Perizinan Berusaha: {scope.perizinan_berusaha}")
        if scope.jangka_waktu:
            lines.append(f"Jangka Waktu: {scope.jangka_waktu}")
        if scope.luas_lahan:
            lines.append(f"Luas Lahan: {scope.luas_lahan}")
        if scope.pb_umku:
            lines.append(f"PB-UMKU: {scope.pb_umku}")
        chunks.append((
            "\n".join(lines),
            {**base_meta, "section": "ruang_lingkup", "skala": scope.skala},
            f"pdf_{safe_code}_scope_{i}",
        ))

    # 3. Enriched PB-UMKU chunks (richest chunks for RAG)
    for i, pb in enumerate(record.pb_umku_detail):
        lines = [
            f"PB-UMKU '{pb.nama_izin}' untuk KBLI {record.kbli_code} ({record.kbli_description}):",
        ]
        if pb.persyaratan:
            lines.append("Persyaratan:")
            lines.extend(f"  - {p}" for p in pb.persyaratan)
        if pb.jangka_waktu:
            lines.append(f"Jangka Waktu Pengurusan: {pb.jangka_waktu}")
        if pb.kewajiban:
            lines.append("Kewajiban Pelaku Usaha:")
            lines.extend(f"  - {k}" for k in pb.kewajiban)
        if pb.parameter_pemenuhan:
            lines.append(f"Parameter Pemenuhan Kewajiban: {pb.parameter_pemenuhan}")
        if pb.kewenangan:
            lines.append(f"Kewenangan Pengawasan: {pb.kewenangan}")
        if pb.waktu_pemenuhan:
            lines.append(f"Waktu Pemenuhan Kewajiban: {pb.waktu_pemenuhan}")
        if pb.biaya:
            lines.append(f"Biaya: {pb.biaya}")
        if pb.sanksi:
            lines.append(f"Sanksi Pelanggaran: {pb.sanksi}")

        pb_name_hash = hashlib.md5(pb.nama_izin.encode()).hexdigest()[:8]
        chunks.append((
            "\n".join(lines),
            {**base_meta, "section": "pb_umku_enriched", "pb_umku_name": pb.nama_izin},
            f"pdf_{safe_code}_pbumku_{pb_name_hash}",
        ))

    # 4. Summary chunk if no granular details exist
    if not chunks:
        summary = f"KBLI {record.kbli_code} ({record.kbli_description})"
        if record.uraian:
            summary += f"\n{record.uraian}"
        chunks.append((
            summary,
            {**base_meta, "section": "summary"},
            f"pdf_{safe_code}_summary",
        ))

    return chunks


def _get_chroma_collection():
    """Return the ChromaDB collection with the project embedding function."""
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    ef = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    return client.get_or_create_collection(
        name=CHROMA_COLLECTION,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )


async def ingestor_agent(
    enriched_records: list[KbliDetailEnriched],
    job_id: int,
) -> int:
    """
    Agent 3: Vectorize enriched records into ChromaDB and update PostgreSQL.
    Returns total chunks upserted.
    """
    logger.info("[Agent 3] Starting Ingestor agent for %d records", len(enriched_records))

    col = await asyncio.get_event_loop().run_in_executor(None, _get_chroma_collection)

    total_chunks = 0
    BATCH_SIZE = 64

    texts, metadatas, ids = [], [], []

    for record in enriched_records:
        for text, meta, doc_id in _generate_chunks(record):
            texts.append(text)
            metadatas.append(meta)
            ids.append(doc_id)

            if len(texts) >= BATCH_SIZE:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda t=texts[:], m=metadatas[:], i=ids[:]: col.upsert(
                        documents=t, metadatas=m, ids=i
                    ),
                )
                total_chunks += len(texts)
                logger.info("[Agent 3] Upserted batch of %d chunks", len(texts))
                texts.clear()
                metadatas.clear()
                ids.clear()

    # Flush remaining
    if texts:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda t=texts[:], m=metadatas[:], i=ids[:]: col.upsert(
                documents=t, metadatas=m, ids=i
            ),
        )
        total_chunks += len(texts)
        logger.info("[Agent 3] Upserted final batch of %d chunks", len(texts))

    # Update kbli_scrape_targets for each processed KBLI
    async with httpx.AsyncClient(timeout=10) as client:
        for record in enriched_records:
            try:
                kbli_chunks = len(_generate_chunks(record))
                await client.post(
                    f"{BACKEND_URL}/api/internal/pipeline/status",
                    json={
                        "kbli_code":        record.kbli_code,
                        "status":           "done",
                        "chunks_upserted":  kbli_chunks,
                        "duration_seconds": 0,
                    },
                    headers=HEADERS,
                )
            except Exception as exc:
                logger.warning(
                    "[Agent 3] Failed to update kbli_scrape_targets for %s: %s",
                    record.kbli_code, exc,
                )

    logger.info("[Agent 3] Done. Total chunks upserted: %d", total_chunks)
    return total_chunks


# ---------------------------------------------------------------------------
# Pipeline Orchestrator
# ---------------------------------------------------------------------------


async def run_pipeline(job_id: int) -> None:
    """
    Main orchestrator: coordinates the 3 agents and lifecycle reporting.

    Timing is tracked per-agent and printed at the end so we can compare
    the new pdfplumber approach vs the old LLM-heavy approach.
    """
    pipeline_start = time.time()

    logger.info("=" * 60)
    logger.info("Starting PDF pipeline [Deterministic+AI Hybrid] | job_id=%d", job_id)
    logger.info("=" * 60)

    # 1. Fetch job details from backend
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BACKEND_URL}/api/internal/pdf-jobs/{job_id}",
            headers=HEADERS,
        )
        resp.raise_for_status()
        job = resp.json()["data"]

    logger.info(
        "Job: %s | KBLI PDF: %s | PB-UMKU PDF: %s",
        job["name"], job["kbli_pdf_path"], job["pb_umku_pdf_path"],
    )

    # Resolve absolute paths within the mounted backend storage
    kbli_path    = BACKEND_STORAGE / job["kbli_pdf_path"]
    pb_umku_path = BACKEND_STORAGE / job["pb_umku_pdf_path"]

    for path in (kbli_path, pb_umku_path):
        if not path.exists():
            raise FileNotFoundError(
                f"PDF not found at {path}. "
                "Ensure backend_storage is mounted in docker-compose.yml."
            )

    # Parse KBLI filter
    kbli_filter: Optional[list[str]] = None
    if job.get("kbli_codes_filter"):
        raw_filter = job["kbli_codes_filter"]
        kbli_filter = [
            c.strip() for c in raw_filter.replace("\n", ",").split(",") if c.strip()
        ]
        logger.info("KBLI filter active: %s", kbli_filter)

    # ── Agent 1: Deterministic pdfplumber extraction ────────────────────────
    await _report_progress(job_id, 5)
    logger.info("--- Agent 1: Deterministic pdfplumber Extraction (NO LLM) ---")

    agent1_start = time.time()
    kbli_df    = extractor_agent(kbli_path, "kbli")
    pbumku_df  = extractor_agent(pb_umku_path, "pb_umku")
    agent1_elapsed = time.time() - agent1_start

    logger.info(
        "⏱ Agent 1 (pdfplumber) completed in %.1f seconds | "
        "KBLI: %d rows, PB-UMKU: %d rows",
        agent1_elapsed, len(kbli_df), len(pbumku_df),
    )

    await _report_progress(job_id, 30)

    # ── Agent 2: Pandas merge + LLM structuring ─────────────────────────────
    logger.info("--- Agent 2: Pandas Merge + LLM Structuring (per-record) ---")

    agent2_start = time.time()
    enriched_records = await structurer_agent(kbli_df, pbumku_df, kbli_filter)
    agent2_elapsed = time.time() - agent2_start

    logger.info(
        "⏱ Agent 2 (pandas+LLM) completed in %.1f seconds | %d records structured",
        agent2_elapsed, len(enriched_records),
    )

    if not enriched_records:
        raise ValueError("Agent 2 returned zero enriched records — check PDF table structure")

    await _report_progress(job_id, 70)

    # ── Agent 3: Ingest to ChromaDB ──────────────────────────────────────────
    logger.info("--- Agent 3: Ingesting to ChromaDB ---")

    agent3_start = time.time()
    total_chunks = await ingestor_agent(enriched_records, job_id)
    agent3_elapsed = time.time() - agent3_start

    logger.info("⏱ Agent 3 (ingestor) completed in %.1f seconds", agent3_elapsed)

    await _report_progress(job_id, 95)

    # Save merged JSON for admin review
    result_json = json.dumps(
        [r.model_dump() for r in enriched_records],
        ensure_ascii=False,
        indent=2,
    )

    # ── Report completion ────────────────────────────────────────────────────
    await _report_complete(
        job_id=job_id,
        chunks=total_chunks,
        kbli_processed=len(enriched_records),
        result_json=result_json,
    )

    pipeline_elapsed = time.time() - pipeline_start

    logger.info("=" * 60)
    logger.info(
        "Pipeline COMPLETE | job_id=%d | KBLI=%d | chunks=%d",
        job_id, len(enriched_records), total_chunks,
    )
    logger.info("⏱ TIMING BREAKDOWN:")
    logger.info("   Agent 1 – pdfplumber extraction : %6.1f s", agent1_elapsed)
    logger.info("   Agent 2 – pandas + LLM          : %6.1f s", agent2_elapsed)
    logger.info("   Agent 3 – ChromaDB ingestor      : %6.1f s", agent3_elapsed)
    logger.info("   ─────────────────────────────────────────")
    logger.info("   TOTAL                            : %6.1f s  (%.1f min)",
                pipeline_elapsed, pipeline_elapsed / 60)
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="BIMA-AI Multi-Agent PDF Pipeline")
    parser.add_argument("--job-id", type=int, required=True, help="ID of the pdf_parse_jobs record")
    args = parser.parse_args()

    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set — aborting")
        sys.exit(1)
    if not API_KEY:
        logger.error("LARAVEL_API_KEY is not set — aborting")
        sys.exit(1)

    try:
        asyncio.run(run_pipeline(args.job_id))
    except Exception as exc:
        logger.exception("Pipeline FAILED | job_id=%d", args.job_id)
        asyncio.run(_report_fail(args.job_id, str(exc)))
        sys.exit(1)


if __name__ == "__main__":
    main()
