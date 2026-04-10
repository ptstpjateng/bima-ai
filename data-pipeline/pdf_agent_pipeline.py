"""
BIMA-AI — Multi-Agent PDF Parsing Pipeline
==========================================

Processes government OSS PDFs (KBLI Detail + PB-UMKU Detail) through a
3-agent pipeline and vectorizes the merged output into ChromaDB.

Agents
------
  Agent 1 (Extractor)   — PDF → structured Markdown using pdfplumber + Gemini 2.5 Flash
  Agent 2 (Structurer)  — Markdown → validated JSON (KbliDetailEnriched) with
                          matching logic between KBLI and PB-UMKU tables.
                          Self-healing: retry loop with error feedback if LLM output
                          fails Pydantic validation.
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
import base64
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
                # Extract text from response
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
        # Remove first and last fence lines
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
# Agent 1 — Extractor (PDF → structured Markdown)
# ---------------------------------------------------------------------------


def _extract_pdf_text(pdf_path: Path) -> list[dict]:
    """
    Extract text and table content from each PDF page using pdfplumber.

    Returns a list of page dicts:
      { page_num, text, table_text, combined }
    """
    pages = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        logger.info("Extracting %d pages from %s", total, pdf_path.name)
        for i, page in enumerate(pdf.pages):
            raw_text = page.extract_text() or ""

            # Extract table data using pdfplumber's table parser
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


async def _gemini_pages_to_markdown(
    pages_content: str,
    doc_type: str,
    chunk_idx: int,
) -> str:
    """Ask Gemini to convert raw page dump to structured Markdown tables."""
    if doc_type == "kbli":
        instructions = (
            "Ekstrak semua entri KBLI dari teks halaman PDF ini.\n"
            "Untuk setiap KBLI, hasilkan:\n"
            "## KBLI [kode] — [nama singkat]\n"
            "**Uraian:** [teks uraian]\n"
            "**Ruang Lingkup:** [tabel per skala usaha]\n"
            "**PB-UMKU Diperlukan:** [daftar nama PB-UMKU]\n"
            "Pertahankan informasi selengkap mungkin. Jika ada tabel, representasikan "
            "sebagai tabel Markdown dengan header yang jelas."
        )
    else:  # pb_umku
        instructions = (
            "Ekstrak semua entri PB-UMKU dari teks halaman PDF ini.\n"
            "Untuk setiap entri PB-UMKU, hasilkan:\n"
            "## [Nama Izin PB-UMKU]\n"
            "| Kolom | Nilai |\n|---|---|\n"
            "| Persyaratan | ... |\n"
            "| Jangka Waktu | ... |\n"
            "| Kewajiban | ... |\n"
            "| Parameter Pemenuhan | ... |\n"
            "| Kewenangan | ... |\n"
            "| Waktu Pemenuhan | ... |\n"
            "| Biaya | ... |\n"
            "| Sanksi | ... |\n"
            "Pertahankan semua detail. Jika sel kosong, tulis '—'."
        )

    prompt = (
        f"Kamu adalah asisten ekstraksi dokumen pemerintah Indonesia yang presisi.\n"
        f"Tugas: {instructions}\n\n"
        f"--- KONTEN PDF (Chunk {chunk_idx + 1}) ---\n"
        f"{pages_content}\n"
        f"--- AKHIR KONTEN ---\n\n"
        f"Hasilkan HANYA Markdown terstruktur tanpa penjelasan tambahan."
    )
    return await _call_gemini(prompt)


async def extractor_agent(pdf_path: Path, doc_type: str) -> str:
    """
    Agent 1: Extract PDF content into structured Markdown.

    Strategy:
      1. pdfplumber extracts text + table cells per page.
      2. Pages are batched (30 per chunk) and sent to Gemini for Markdown formatting.
      3. Results are concatenated with page-break markers.

    Args:
        pdf_path: Absolute path to the PDF file.
        doc_type: 'kbli' | 'pb_umku' — drives the extraction prompt.

    Returns:
        Combined Markdown string.
    """
    logger.info("[Agent 1] Extracting %s PDF: %s", doc_type.upper(), pdf_path.name)
    pages = _extract_pdf_text(pdf_path)

    CHUNK_SIZE = 30
    all_markdown: list[str] = []

    page_chunks = [pages[i: i + CHUNK_SIZE] for i in range(0, len(pages), CHUNK_SIZE)]
    logger.info("[Agent 1] Processing %d chunks of ≤%d pages", len(page_chunks), CHUNK_SIZE)

    for chunk_idx, chunk in enumerate(page_chunks):
        combined_text = "\n\n".join(p["combined"] for p in chunk)
        total_chars = sum(p["char_count"] for p in chunk)

        logger.info(
            "[Agent 1] Chunk %d/%d | pages %d–%d | ~%d chars",
            chunk_idx + 1, len(page_chunks),
            chunk[0]["page_num"], chunk[-1]["page_num"],
            total_chars,
        )

        if total_chars < 500:
            # Skip nearly-empty pages (cover pages, blank pages)
            logger.debug("[Agent 1] Chunk %d skipped (too sparse)", chunk_idx + 1)
            continue

        markdown_chunk = await _gemini_pages_to_markdown(combined_text, doc_type, chunk_idx)
        all_markdown.append(markdown_chunk)

        # Polite delay to avoid hammering Gemini
        await asyncio.sleep(1.5)

    result = "\n\n---\n\n".join(all_markdown)
    logger.info("[Agent 1] Extraction complete. Output: %d chars", len(result))
    return result


# ---------------------------------------------------------------------------
# Agent 2 — Structurer & Matcher
# ---------------------------------------------------------------------------


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


async def _extract_kbli_list(
    kbli_markdown: str,
    kbli_filter: Optional[list[str]],
) -> list[KbliExtracted]:
    """Agent 2 Step A: Parse KBLI Markdown → list of KbliExtracted."""
    filter_clause = ""
    if kbli_filter:
        filter_clause = (
            f"\nPERHATIAN: Hanya ekstrak KBLI dengan kode berikut: {', '.join(kbli_filter)}. "
            "Abaikan kode KBLI lainnya."
        )

    prompt = (
        f"Kamu adalah parser data KBLI yang presisi.\n"
        f"Berikut adalah Markdown hasil ekstraksi dari PDF KBLI Detail.\n"
        f"Tugasmu: ekstrak semua entri KBLI dan kembalikan sebagai JSON.\n"
        f"{filter_clause}\n\n"
        f"Format JSON yang diharapkan:\n"
        f"{{\n"
        f'  "kbli_list": [\n'
        f"    {{\n"
        f'      "kbli_code": "56102",\n'
        f'      "kbli_description": "Warung/Kedai Makan",\n'
        f'      "uraian": "Kelompok ini mencakup...",\n'
        f'      "ruang_lingkup": [\n'
        f'        {{"skala": "Mikro", "tingkat_risiko": "Rendah", "perizinan_berusaha": "NIB", "jangka_waktu": "...", "pb_umku": "..."}}\n'
        f"      ],\n"
        f'      "pb_umku_names": ["Sertifikat Laik Higiene Sanitasi Jasa Boga", "..."]\n'
        f"    }}\n"
        f"  ]\n"
        f"}}\n\n"
        f"PENTING:\n"
        f"- pb_umku_names harus berisi nama-nama PB-UMKU yang SPESIFIK (bukan kategori umum)\n"
        f"- Kembalikan HANYA JSON, tanpa markdown fence atau teks lain\n"
        f"- Jika tidak ada data KBLI ditemukan, kembalikan {{\"kbli_list\": []}}\n\n"
        f"--- MARKDOWN KBLI ---\n"
        f"{kbli_markdown[:60000]}\n"  # safety truncation
        f"--- AKHIR ---"
    )

    result = await _structured_gemini_call(prompt, KbliListResponse)
    logger.info("[Agent 2A] Extracted %d KBLI entries", len(result.kbli_list))
    return result.kbli_list


async def _extract_pb_umku_table(
    pb_umku_markdown: str,
    pb_umku_names_to_find: list[str],
) -> dict[str, PbUmkuDetail]:
    """
    Agent 2 Step B: Parse PB-UMKU Markdown → dict of name → PbUmkuDetail.

    Processes the markdown in chunks to handle very large PB-UMKU tables.
    """
    if not pb_umku_names_to_find:
        logger.info("[Agent 2B] No PB-UMKU names to look up — skipping")
        return {}

    names_str = "\n".join(f"- {n}" for n in pb_umku_names_to_find)

    # PB-UMKU PDFs can be very long — chunk the markdown
    CHUNK_SIZE = 40_000  # characters
    chunks = [
        pb_umku_markdown[i: i + CHUNK_SIZE]
        for i in range(0, len(pb_umku_markdown), CHUNK_SIZE)
    ]
    logger.info("[Agent 2B] Looking up %d PB-UMKU names across %d markdown chunks", len(pb_umku_names_to_find), len(chunks))

    all_found: dict[str, PbUmkuDetail] = {}

    for chunk_idx, chunk in enumerate(chunks):
        # Only process chunk if it mentions at least one target name
        chunk_lower = chunk.lower()
        relevant = any(n.lower()[:20] in chunk_lower for n in pb_umku_names_to_find)
        if not relevant:
            continue

        prompt = (
            f"Kamu adalah parser tabel PB-UMKU yang presisi.\n"
            f"Cari entri PB-UMKU berikut dalam teks Markdown ini:\n{names_str}\n\n"
            f"Untuk setiap yang ditemukan, ekstrak:\n"
            f"{{\n"
            f'  "pb_umku_list": [\n'
            f"    {{\n"
            f'      "nama_izin": "Nama lengkap PB-UMKU",\n'
            f'      "persyaratan": ["syarat 1", "syarat 2"],\n'
            f'      "jangka_waktu": "...",\n'
            f'      "kewajiban": ["kewajiban 1"],\n'
            f'      "parameter_pemenuhan": "...",\n'
            f'      "kewenangan": "...",\n'
            f'      "waktu_pemenuhan": "...",\n'
            f'      "biaya": "...",\n'
            f'      "sanksi": "..."\n'
            f"    }}\n"
            f"  ]\n"
            f"}}\n\n"
            f"PENTING:\n"
            f"- Hanya ekstrak PB-UMKU yang namanya mirip dengan daftar di atas\n"
            f"- Jika tidak ditemukan dalam chunk ini, kembalikan {{\"pb_umku_list\": []}}\n"
            f"- Kembalikan HANYA JSON\n\n"
            f"--- MARKDOWN CHUNK {chunk_idx + 1} ---\n"
            f"{chunk}\n"
            f"--- AKHIR ---"
        )

        try:
            result = await _structured_gemini_call(prompt, PbUmkuListResponse, max_retries=2)
            for pb in result.pb_umku_list:
                if pb.nama_izin not in all_found:
                    all_found[pb.nama_izin] = pb
                    logger.info("[Agent 2B] Found: %s", pb.nama_izin)
        except Exception as exc:
            logger.warning("[Agent 2B] Chunk %d failed: %s", chunk_idx + 1, exc)

        await asyncio.sleep(1.5)

    logger.info("[Agent 2B] Found %d/%d PB-UMKU entries", len(all_found), len(pb_umku_names_to_find))
    return all_found


def _match_and_merge(
    kbli_list: list[KbliExtracted],
    pb_umku_map: dict[str, PbUmkuDetail],
) -> list[KbliDetailEnriched]:
    """
    Agent 2 Step C: Match each KBLI's pb_umku_names against pb_umku_map using
    fuzzy matching and inject the details.
    """
    enriched: list[KbliDetailEnriched] = []
    candidate_names = list(pb_umku_map.keys())

    for kbli in kbli_list:
        matched: list[PbUmkuDetail] = []

        for name in kbli.pb_umku_names:
            # Try exact match first, then fuzzy
            if name in pb_umku_map:
                matched.append(pb_umku_map[name])
                logger.debug("[Agent 2C] Exact match: %s → %s", kbli.kbli_code, name)
            else:
                fuzzy = _fuzzy_match(name, candidate_names, threshold=0.75)
                if fuzzy:
                    matched.append(pb_umku_map[fuzzy])
                    logger.info(
                        "[Agent 2C] Fuzzy match: %s — '%s' → '%s'",
                        kbli.kbli_code, name, fuzzy,
                    )
                else:
                    logger.debug("[Agent 2C] No match for: %s in KBLI %s", name, kbli.kbli_code)

        enriched.append(KbliDetailEnriched(
            kbli_code=kbli.kbli_code,
            kbli_description=kbli.kbli_description,
            uraian=kbli.uraian,
            ruang_lingkup=kbli.ruang_lingkup,
            pb_umku_names=kbli.pb_umku_names,
            pb_umku_detail=matched,
        ))

    logger.info(
        "[Agent 2C] Merged %d KBLI records | total PB-UMKU injected: %d",
        len(enriched),
        sum(len(r.pb_umku_detail) for r in enriched),
    )
    return enriched


async def structurer_agent(
    kbli_markdown: str,
    pb_umku_markdown: str,
    kbli_filter: Optional[list[str]],
) -> list[KbliDetailEnriched]:
    """
    Agent 2: Parse and match KBLI Detail and PB-UMKU Detail Markdowns.

    Returns a list of fully enriched KBLI records.
    """
    logger.info("[Agent 2] Starting Structurer agent")

    # Step A: Extract KBLI list from KBLI Markdown
    kbli_list = await _extract_kbli_list(kbli_markdown, kbli_filter)
    if not kbli_list:
        raise ValueError("Agent 2A: No KBLI entries extracted from KBLI PDF")

    # Collect all PB-UMKU names that need to be looked up
    all_pb_umku_names: list[str] = []
    for kbli in kbli_list:
        all_pb_umku_names.extend(kbli.pb_umku_names)
    unique_pb_names = list(dict.fromkeys(all_pb_umku_names))  # preserve order, dedupe

    # Step B: Extract PB-UMKU details from PB-UMKU Markdown
    pb_umku_map = await _extract_pb_umku_table(pb_umku_markdown, unique_pb_names)

    # Step C: Fuzzy-match and merge
    enriched = _match_and_merge(kbli_list, pb_umku_map)

    return enriched


# ---------------------------------------------------------------------------
# Agent 3 — Ingestor (JSON → ChromaDB + PostgreSQL)
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

            # Upsert in batches
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
                logger.warning("[Agent 3] Failed to update kbli_scrape_targets for %s: %s", record.kbli_code, exc)

    logger.info("[Agent 3] Done. Total chunks upserted: %d", total_chunks)
    return total_chunks


# ---------------------------------------------------------------------------
# Pipeline Orchestrator
# ---------------------------------------------------------------------------


async def run_pipeline(job_id: int) -> None:
    """
    Main orchestrator: coordinates the 3 agents and lifecycle reporting.
    """
    logger.info("=" * 60)
    logger.info("Starting PDF pipeline | job_id=%d", job_id)
    logger.info("=" * 60)

    # 1. Fetch job details from backend
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BACKEND_URL}/api/internal/pdf-jobs/{job_id}",
            headers=HEADERS,
        )
        resp.raise_for_status()
        job = resp.json()["data"]

    logger.info("Job: %s | KBLI PDF: %s | PB-UMKU PDF: %s",
                job["name"], job["kbli_pdf_path"], job["pb_umku_pdf_path"])

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

    # ── Agent 1: Extract both PDFs ──────────────────────────────────────────
    await _report_progress(job_id, 5)
    logger.info("--- Agent 1: Extracting KBLI PDF ---")
    kbli_markdown = await extractor_agent(kbli_path, "kbli")

    await _report_progress(job_id, 25)
    logger.info("--- Agent 1: Extracting PB-UMKU PDF ---")
    pb_umku_markdown = await extractor_agent(pb_umku_path, "pb_umku")

    await _report_progress(job_id, 45)

    # ── Agent 2: Structure & Match ──────────────────────────────────────────
    logger.info("--- Agent 2: Structuring & Matching ---")
    enriched_records = await structurer_agent(kbli_markdown, pb_umku_markdown, kbli_filter)

    if not enriched_records:
        raise ValueError("Agent 2 returned zero enriched records — check PDF content")

    await _report_progress(job_id, 70)

    # ── Agent 3: Ingest to ChromaDB ─────────────────────────────────────────
    logger.info("--- Agent 3: Ingesting to ChromaDB ---")
    total_chunks = await ingestor_agent(enriched_records, job_id)

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

    logger.info(
        "Pipeline COMPLETE | job_id=%d | KBLI=%d | chunks=%d",
        job_id, len(enriched_records), total_chunks,
    )


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
        # Best-effort failure report
        asyncio.run(_report_fail(args.job_id, str(exc)))
        sys.exit(1)


if __name__ == "__main__":
    main()
