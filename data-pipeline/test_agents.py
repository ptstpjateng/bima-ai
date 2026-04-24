"""
Quick agent smoke-test — no backend API, no Filament job required.
Uses Ollama/gemma4 (same as the KBLI crawler) instead of Gemini.

Usage (inside container):
    python test_agents.py --pdf /path/to/lampiran-kbli.pdf --max-pages 30 --limit 5

What it tests:
  ✓ Agent 1  — pdfplumber extraction + Ollama Markdown conversion
  ✓ Agent 2A — Ollama JSON structuring + Pydantic self-healing
  ✓ Agent 3  — ChromaDB upsert (real write, verifiable afterwards)

Agent 2B/2C (PB-UMKU matching) is skipped unless --pb-umku-pdf is provided.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from ollama import AsyncClient as OllamaClient
from pydantic import BaseModel, ValidationError

# Reuse schema + pdfplumber extraction from the main pipeline
sys.path.insert(0, str(Path(__file__).parent))
from pdf_agent_pipeline import (
    _extract_pdf_text,
    _match_and_merge,
    ingestor_agent,
    KbliDetailEnriched,
    KbliListResponse,
    PbUmkuListResponse,
    CHROMA_DB_PATH,
    _strip_markdown_fences,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_agents")

OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",  "http://172.19.0.1:11435")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4")


# ---------------------------------------------------------------------------
# Ollama LLM helpers (mirrors _call_gemini / _structured_gemini_call)
# ---------------------------------------------------------------------------

async def _call_ollama(prompt: str) -> str:
    """Call Ollama and return raw text response."""
    logger.debug("[LLM] Sending %d chars to Ollama (%s)", len(prompt), OLLAMA_MODEL)
    client = OllamaClient(host=OLLAMA_HOST)
    response = await client.generate(
        model=OLLAMA_MODEL,
        prompt=prompt,
        stream=False,
        options={"temperature": 0.1, "num_predict": 8192},
    )
    return response["response"].strip()


async def _structured_ollama_call(
    prompt: str,
    response_model: type[BaseModel],
    max_retries: int = 3,
) -> BaseModel:
    """Call Ollama, validate against Pydantic schema. Self-heal on failure."""
    last_error: Optional[str] = None

    for attempt in range(max_retries):
        try:
            healing = prompt
            if attempt > 0 and last_error:
                healing = (
                    f"{prompt}\n\n"
                    f"⚠️ PERBAIKAN DIPERLUKAN: Respons sebelumnya gagal validasi:\n"
                    f"{last_error}\n\n"
                    f"Kembalikan JSON yang valid sesuai schema. Jangan tambahkan teks lain."
                )
            raw   = await _call_ollama(healing)
            clean = _strip_markdown_fences(raw)
            data  = json.loads(clean)
            return response_model.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)[:500]
            logger.warning("Attempt %d/%d validation failed: %s", attempt + 1, max_retries, last_error)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 * (attempt + 1))

    raise ValueError(f"Structured extraction failed after {max_retries} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Agent 1 — PDF extractor (limited to max_pages, uses Ollama)
# ---------------------------------------------------------------------------

async def _pages_to_markdown_ollama(pages_content: str, chunk_idx: int, kbli_codes: list[str]) -> str:
    codes_hint = ", ".join(kbli_codes) if kbli_codes else "tidak diketahui"
    prompt = (
        "Kamu adalah asisten ekstraksi tabel perizinan usaha dari dokumen pemerintah Indonesia.\n"
        "Dokumen ini adalah Lampiran PP 28/2025 — berisi tabel besar perizinan usaha berbasis risiko.\n"
        "Teks diekstrak dari PDF hasil scan sehingga ada karakter OCR yang rusak — abaikan ini.\n\n"
        f"Kode KBLI yang terdeteksi di halaman ini: {codes_hint}\n\n"
        "Untuk SETIAP kode KBLI yang kamu temukan, hasilkan Markdown:\n"
        "## KBLI [kode] — [nama/judul usaha]\n"
        "**Uraian:** [deskripsi singkat jenis usaha]\n"
        "**Skala dan Perizinan:**\n"
        "| Skala | Tingkat Risiko | Jenis Perizinan | Jangka Waktu |\n"
        "|---|---|---|---|\n"
        "| [Mikro/Kecil/Menengah/Besar] | [Rendah/Menengah/Tinggi] | [NIB/Sertifikat/Izin] | [durasi] |\n"
        "**PB-UMKU Diperlukan:** [nama-nama izin PB-UMKU yang disebutkan]\n"
        "**Persyaratan Utama:** [ringkasan persyaratan]\n\n"
        "ATURAN:\n"
        "- Fokus hanya pada data tabel (Kode, Judul, Skala, Tingkat Risiko, Perizinan, Persyaratan, PB-UMKU)\n"
        "- Abaikan header halaman, nomor halaman, dan teks header tabel yang rusak\n"
        "- Jika ada beberapa skala untuk satu KBLI, buat satu baris tabel per skala\n"
        "- Hasilkan HANYA Markdown, tidak ada penjelasan\n\n"
        f"--- KONTEN PDF (Chunk {chunk_idx + 1}) ---\n"
        f"{pages_content}\n"
        "--- AKHIR KONTEN ---"
    )
    return await _call_ollama(prompt)


async def extractor_agent_limited(pdf_path: Path, max_pages: int) -> str:
    """Agent 1: PDF → Markdown, limited to first max_pages pages."""
    import re
    logger.info("[Agent 1] Extracting KBLI PDF (max %d pages): %s", max_pages, pdf_path.name)

    all_pages = _extract_pdf_text(pdf_path)
    logger.info("[Agent 1] PDF has %d total pages — processing first %d", len(all_pages), max_pages)
    pages = all_pages[:max_pages]

    # Detect which KBLI codes appear in each page to hint the LLM
    def find_codes(text: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r'\b(\d{5})\b', text)))

    CHUNK_SIZE = 30
    all_markdown = []
    page_chunks = [pages[i: i + CHUNK_SIZE] for i in range(0, len(pages), CHUNK_SIZE)]
    logger.info("[Agent 1] %d chunk(s) to send to Ollama", len(page_chunks))

    for idx, chunk in enumerate(page_chunks):
        combined = "\n\n".join(p["combined"] for p in chunk)
        total_chars = sum(p["char_count"] for p in chunk)
        codes_in_chunk = find_codes(combined)
        logger.info("[Agent 1] Chunk %d/%d | pages %d–%d | ~%d chars | codes=%s",
                    idx + 1, len(page_chunks),
                    chunk[0]["page_num"], chunk[-1]["page_num"], total_chars,
                    codes_in_chunk[:8])
        if total_chars < 300:
            continue
        md = await _pages_to_markdown_ollama(combined, idx, codes_in_chunk[:8])
        all_markdown.append(md)
        await asyncio.sleep(1.0)

    result = "\n\n---\n\n".join(all_markdown)
    logger.info("[Agent 1] Done. Output: %d chars", len(result))
    return result


# ---------------------------------------------------------------------------
# Agent 2A — KBLI structurer (uses Ollama)
# ---------------------------------------------------------------------------

async def extract_kbli_list_ollama(
    kbli_markdown: str,
    kbli_filter: Optional[list[str]],
    limit: int,
) -> list:
    filter_clause = ""
    if kbli_filter:
        filter_clause = (
            f"\nPERHATIAN: Hanya ekstrak KBLI dengan kode: {', '.join(kbli_filter)}."
        )
    else:
        filter_clause = f"\nPERHATIAN: Ekstrak maksimal {limit} entri KBLI pertama. Pilih entri yang paling lengkap datanya."

    prompt = (
        "Kamu adalah parser data KBLI yang presisi.\n"
        "Berikut adalah Markdown hasil ekstraksi dari tabel perizinan usaha PP 28/2025.\n"
        "Tugasmu: ekstrak entri KBLI dan kembalikan sebagai JSON.\n"
        "Setiap entri ## KBLI [kode] adalah satu record.\n"
        f"{filter_clause}\n\n"
        "Format JSON yang diharapkan:\n"
        '{\n  "kbli_list": [\n'
        '    {\n'
        '      "kbli_code": "56102",\n'
        '      "kbli_description": "Warung/Kedai Makan",\n'
        '      "uraian": "Kelompok ini mencakup...",\n'
        '      "ruang_lingkup": [\n'
        '        {"skala": "Mikro", "tingkat_risiko": "Rendah", "perizinan_berusaha": "NIB", "pb_umku": "..."}\n'
        "      ],\n"
        '      "pb_umku_names": ["Nama PB-UMKU 1", "..."]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "PENTING:\n"
        "- Kembalikan HANYA JSON, tidak ada teks lain\n"
        "- pb_umku_names harus berisi nama-nama izin yang spesifik\n"
        "- Jika field tidak ada, gunakan string kosong atau array kosong\n\n"
        f"--- MARKDOWN KBLI ---\n{kbli_markdown}\n--- AKHIR ---"
    )

    result = await _structured_ollama_call(prompt, KbliListResponse)
    return result.kbli_list


# ---------------------------------------------------------------------------
# Main test flow
# ---------------------------------------------------------------------------

async def run_test(
    kbli_pdf: Path,
    max_pages: int,
    kbli_limit: int,
    kbli_filter: Optional[list[str]],
    skip_chroma: bool,
) -> None:
    logger.info("=" * 60)
    logger.info("BIMA-AI Agent Smoke Test  [LLM: %s @ %s]", OLLAMA_MODEL, OLLAMA_HOST)
    logger.info("  KBLI PDF  : %s", kbli_pdf)
    logger.info("  Max pages : %d", max_pages)
    logger.info("  KBLI limit: %d", kbli_limit)
    logger.info("  Filter    : %s", kbli_filter or "auto (first N from PDF)")
    logger.info("=" * 60)

    # ── Quick connectivity check ──────────────────────────────────────────────
    logger.info("\n▶ Checking Ollama connectivity...")
    try:
        ping = await _call_ollama("Balas dengan satu kata: OK")
        logger.info("[Ollama] Response: %s", ping.strip()[:80])
    except Exception as exc:
        logger.error("[Ollama] Connection failed: %s", exc)
        logger.error("Is the ollama-proxy screen session running on the host?")
        logger.error("  ssh bima-vps 'screen -dmS ollama-proxy python3 ~/ollama-proxy.py'")
        sys.exit(1)

    # ── Agent 1 ───────────────────────────────────────────────────────────────
    logger.info("\n▶ AGENT 1 — PDF Extractor (pdfplumber + Ollama)")
    kbli_markdown = await extractor_agent_limited(kbli_pdf, max_pages)

    preview = kbli_markdown[:600].replace("\n", " ↵ ")
    logger.info("\n[Agent 1 preview] %s...\n", preview)

    # ── Agent 2A ──────────────────────────────────────────────────────────────
    logger.info("\n▶ AGENT 2A — KBLI Structurer (Ollama + Pydantic self-heal)")
    kbli_list = await extract_kbli_list_ollama(kbli_markdown, kbli_filter, kbli_limit)

    if not kbli_list:
        logger.error("Agent 2A returned 0 KBLI records — try --max-pages 50 or check PDF")
        sys.exit(1)

    if not kbli_filter:
        kbli_list = kbli_list[:kbli_limit]

    logger.info("\n[Agent 2A] Extracted %d KBLI records:", len(kbli_list))
    for k in kbli_list:
        logger.info("  %-8s %-40s  pb_umku=%d  ruang_lingkup=%d",
                    k.kbli_code, k.kbli_description[:40],
                    len(k.pb_umku_names), len(k.ruang_lingkup))

    # ── Wrap as enriched (no PB-UMKU for this test) ───────────────────────────
    enriched_records = [
        KbliDetailEnriched(
            kbli_code=k.kbli_code,
            kbli_description=k.kbli_description,
            uraian=k.uraian,
            ruang_lingkup=k.ruang_lingkup,
            pb_umku_names=k.pb_umku_names,
            pb_umku_detail=[],
        )
        for k in kbli_list
    ]

    logger.info("\n[Agent 2 — first record preview]\n%s\n",
                json.dumps(enriched_records[0].model_dump(), ensure_ascii=False, indent=2)[:1000])

    # ── Agent 3 ───────────────────────────────────────────────────────────────
    if skip_chroma:
        logger.info("\n▶ AGENT 3 — ChromaDB Ingestor [SKIPPED via --skip-chroma]")
    else:
        logger.info("\n▶ AGENT 3 — ChromaDB Ingestor")

        # Monkey-patch progress/status reporting to no-ops
        import pdf_agent_pipeline as _pipeline
        async def _noop(*args, **kwargs): pass
        _pipeline._report_progress = _noop
        _pipeline._report_complete = _noop

        total_chunks = await ingestor_agent(enriched_records, job_id=0)
        logger.info("[Agent 3] Upserted %d chunks to ChromaDB at %s", total_chunks, CHROMA_DB_PATH)

        # Verification query
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        ef  = SentenceTransformerEmbeddingFunction(model_name="paraphrase-multilingual-MiniLM-L12-v2")
        col = chromadb.PersistentClient(path=CHROMA_DB_PATH).get_collection(
            name="oss_regulations", embedding_function=ef
        )
        first_code = enriched_records[0].kbli_code
        sample = col.query(query_texts=[f"KBLI {first_code}"], n_results=3)
        logger.info("[Agent 3] Verification query for KBLI %s → %d hits", first_code, len(sample["documents"][0]))
        for doc in sample["documents"][0]:
            logger.info("  » %s", doc[:120])

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("✅ SMOKE TEST COMPLETE")
    logger.info("  KBLIs extracted : %d", len(enriched_records))
    if not skip_chroma:
        logger.info("  ChromaDB        : ✓ verified")
    logger.info("=" * 60)

    out = Path(__file__).parent / "data" / "test_output.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps([r.model_dump() for r in enriched_records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Full JSON → %s", out)


def main():
    parser = argparse.ArgumentParser(description="BIMA-AI Agent Smoke Test (Ollama/gemma4)")
    parser.add_argument("--pdf",        required=True, help="Path to KBLI Detail PDF")
    parser.add_argument("--max-pages",  type=int, default=30,  help="Max pages to process (default: 30)")
    parser.add_argument("--limit",      type=int, default=5,   help="Max KBLIs to extract (default: 5)")
    parser.add_argument("--filter",     nargs="*",             help="Specific KBLI codes e.g. --filter 56102 56103")
    parser.add_argument("--skip-chroma", action="store_true",  help="Skip ChromaDB ingestion")
    args = parser.parse_args()

    asyncio.run(run_test(
        kbli_pdf=Path(args.pdf),
        max_pages=args.max_pages,
        kbli_limit=args.limit,
        kbli_filter=args.filter,
        skip_chroma=args.skip_chroma,
    ))


if __name__ == "__main__":
    main()
