"""
Vectorization pipeline for BIMA-AI regulatory data.

Orchestrates the full ETL flow:
  1. Scrape public DPMPTSP/OSS web pages.
  2. Parse local regulatory PDFs from data/pdfs/.
  3. Chunk text with LangChain's RecursiveCharacterTextSplitter.
  4. Embed chunks with a multilingual HuggingFace model.
  5. Upsert into a persistent local ChromaDB collection.

Run as a standalone script:
    python -m pipeline.vectorize

Or import and call run_pipeline() from another module / scheduler.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import chromadb
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings

# Project root is one level above this file.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Ensure sibling packages (extractors/) are importable when running directly.
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from extractors.pdf_parser import PDFParser
from extractors.web_scraper import WebScraper

# ---------------------------------------------------------------------------
# Logging — configured for standalone runs; library callers can override.
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — edit these constants or pass overrides to run_pipeline().
# ---------------------------------------------------------------------------

# Public OSS/DPMPTSP URLs to scrape.  Add or remove as needed.
DEFAULT_URLS: list[str] = [
    "https://oss.go.id/informasi/kbli-berbasis-risiko",
    "https://oss.go.id/informasi/panduan",
    "https://dpmptsp.jabarprov.go.id/layanan/perizinan-berusaha",
]

CHROMA_DIR = str(_PROJECT_ROOT / "chroma_db")
COLLECTION_NAME = "oss_regulations"

# paraphrase-multilingual-MiniLM-L12-v2 supports 50+ languages including
# Indonesian (Bahasa Indonesia) and outperforms the English-only MiniLM-L6-v2
# on multilingual regulatory text.
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

CHUNK_SIZE = 1000       # characters per chunk
CHUNK_OVERLAP = 100     # overlap to preserve cross-boundary context
UPSERT_BATCH_SIZE = 64  # max docs per ChromaDB upsert call


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------

@dataclass
class RawDocument:
    """A single piece of source text before chunking."""
    text: str
    source: str          # URL or file path string
    source_type: str     # "web" | "pdf"
    page: Optional[int] = None   # PDF page number; None for web docs


@dataclass
class PipelineStats:
    sources_attempted: int = 0
    sources_succeeded: int = 0
    raw_docs_produced: int = 0
    chunks_created: int = 0
    chunks_upserted: int = 0
    chunks_failed: int = 0
    duration_seconds: float = 0.0

    def log_summary(self) -> None:
        logger.info("=" * 60)
        logger.info("PIPELINE SUMMARY")
        logger.info("  Sources attempted : %d", self.sources_attempted)
        logger.info("  Sources succeeded : %d", self.sources_succeeded)
        logger.info("  Raw documents     : %d", self.raw_docs_produced)
        logger.info("  Chunks created    : %d", self.chunks_created)
        logger.info("  Chunks upserted   : %d", self.chunks_upserted)
        logger.info("  Chunks failed     : %d", self.chunks_failed)
        logger.info("  Duration          : %.1fs", self.duration_seconds)
        logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------

def _collect_web_documents(urls: list[str], stats: PipelineStats) -> list[RawDocument]:
    """Scrape all URLs and convert successful results to RawDocuments."""
    if not urls:
        logger.info("No URLs configured — skipping web scraping.")
        return []

    logger.info("--- Stage 1a: Web Scraping (%d URL(s)) ---", len(urls))
    scraper = WebScraper(urls=urls)
    results = scraper.scrape_all()

    docs: list[RawDocument] = []
    for result in results:
        stats.sources_attempted += 1
        if result.success and result.text:
            stats.sources_succeeded += 1
            docs.append(
                RawDocument(
                    text=result.text,
                    source=result.url,
                    source_type="web",
                )
            )
        else:
            logger.warning(
                "Skipping %s — reason: %s", result.url, result.error or "unknown"
            )

    logger.info("Web scraping complete: %d/%d URL(s) produced text.", len(docs), len(urls))
    return docs


def _collect_pdf_documents(stats: PipelineStats) -> list[RawDocument]:
    """Parse all PDFs and convert successful pages to RawDocuments."""
    logger.info("--- Stage 1b: PDF Parsing ---")
    parser = PDFParser()
    doc_results = parser.parse_all()

    if not doc_results:
        logger.info("No PDFs found or parsed.")
        return []

    docs: list[RawDocument] = []
    for doc_result in doc_results:
        stats.sources_attempted += 1
        if not doc_result.success:
            logger.warning(
                "Skipping %s — reason: %s",
                doc_result.path.name, doc_result.error or "unknown",
            )
            continue

        stats.sources_succeeded += 1
        source_str = str(doc_result.path)

        for page_result in doc_result.pages:
            if page_result.success and page_result.text:
                docs.append(
                    RawDocument(
                        text=page_result.text,
                        source=source_str,
                        source_type="pdf",
                        page=page_result.page_number,
                    )
                )
            elif not page_result.success:
                logger.debug(
                    "Skipping page %d of %s: %s",
                    page_result.page_number,
                    doc_result.path.name,
                    page_result.error,
                )

        logger.info(
            "%s — %d/%d pages with usable text.",
            doc_result.path.name,
            doc_result.successful_pages,
            doc_result.total_pages,
        )

    logger.info("PDF parsing complete: %d page-level document(s) produced.", len(docs))
    return docs


def _chunk_documents(
    raw_docs: list[RawDocument],
    stats: PipelineStats,
) -> tuple[list[str], list[dict], list[str]]:
    """
    Split raw documents into chunks.

    Returns three parallel lists ready for ChromaDB upsert:
      texts     — chunk text strings
      metadatas — one metadata dict per chunk
      ids       — deterministic, stable chunk IDs (sha256-based)
    """
    logger.info("--- Stage 2: Chunking (%d raw document(s)) ---", len(raw_docs))

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        length_function=len,
        # Prefer splitting on paragraph/sentence boundaries for regulatory text.
        separators=["\n\n", "\n", ". ", ".", " ", ""],
    )

    texts: list[str] = []
    metadatas: list[dict] = []
    ids: list[str] = []

    for raw_doc in raw_docs:
        try:
            chunks = splitter.split_text(raw_doc.text)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Chunking failed for source '%s': %s", raw_doc.source, exc, exc_info=True
            )
            continue

        for chunk_index, chunk in enumerate(chunks):
            if not chunk.strip():
                continue

            metadata = {
                "source": raw_doc.source,
                "source_type": raw_doc.source_type,
                "chunk_index": chunk_index,
            }
            if raw_doc.page is not None:
                metadata["page"] = raw_doc.page

            # Stable ID: any identical chunk from the same source position will
            # map to the same ID, making repeated pipeline runs idempotent.
            fingerprint = f"{raw_doc.source}::{raw_doc.page}::{chunk_index}::{chunk[:64]}"
            chunk_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:32]

            texts.append(chunk)
            metadatas.append(metadata)
            ids.append(chunk_id)

    stats.chunks_created = len(texts)
    logger.info("Chunking complete: %d chunk(s) created.", stats.chunks_created)
    return texts, metadatas, ids


def _build_embedding_function() -> HuggingFaceEmbeddings:
    """Load the HuggingFace embedding model (downloads on first run)."""
    logger.info("Loading embedding model: %s", EMBEDDING_MODEL)
    try:
        embeddings = HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
        logger.info("Embedding model loaded successfully.")
        return embeddings
    except Exception as exc:
        logger.critical(
            "Failed to load embedding model '%s': %s", EMBEDDING_MODEL, exc, exc_info=True
        )
        raise


def _get_or_create_collection(
    client: chromadb.PersistentClient,
) -> chromadb.Collection:
    """Return the target collection, creating it if it does not yet exist."""
    try:
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            "Collection '%s' ready (%d existing item(s)).",
            COLLECTION_NAME,
            collection.count(),
        )
        return collection
    except Exception as exc:
        logger.critical(
            "Cannot access ChromaDB collection '%s': %s",
            COLLECTION_NAME, exc, exc_info=True,
        )
        raise


def _upsert_in_batches(
    collection: chromadb.Collection,
    embeddings_model: HuggingFaceEmbeddings,
    texts: list[str],
    metadatas: list[dict],
    ids: list[str],
    stats: PipelineStats,
) -> None:
    """
    Embed and upsert chunks in batches.

    Batching prevents memory spikes on large corpora and gives us
    per-batch error isolation — a single bad batch does not abort the run.
    """
    logger.info(
        "--- Stage 3: Embedding & Upserting (%d chunk(s), batch size %d) ---",
        len(texts), UPSERT_BATCH_SIZE,
    )

    total = len(texts)
    for batch_start in range(0, total, UPSERT_BATCH_SIZE):
        batch_end = min(batch_start + UPSERT_BATCH_SIZE, total)
        batch_num = batch_start // UPSERT_BATCH_SIZE + 1
        batch_total = (total + UPSERT_BATCH_SIZE - 1) // UPSERT_BATCH_SIZE

        batch_texts = texts[batch_start:batch_end]
        batch_meta = metadatas[batch_start:batch_end]
        batch_ids = ids[batch_start:batch_end]

        try:
            logger.info(
                "  Batch %d/%d — embedding %d chunk(s) [%d–%d]…",
                batch_num, batch_total, len(batch_texts), batch_start + 1, batch_end,
            )
            vectors = embeddings_model.embed_documents(batch_texts)

            collection.upsert(
                ids=batch_ids,
                embeddings=vectors,
                documents=batch_texts,
                metadatas=batch_meta,
            )
            stats.chunks_upserted += len(batch_texts)
            logger.info(
                "  Batch %d/%d — upserted successfully.", batch_num, batch_total
            )

        except Exception as exc:  # noqa: BLE001
            failed_count = len(batch_texts)
            stats.chunks_failed += failed_count
            logger.error(
                "  Batch %d/%d — FAILED (%d chunk(s) lost): %s",
                batch_num, batch_total, failed_count, exc, exc_info=True,
            )
            # Continue with the next batch rather than aborting the pipeline.

    logger.info(
        "Upsert complete: %d succeeded, %d failed.",
        stats.chunks_upserted, stats.chunks_failed,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_pipeline(
    urls: Optional[list[str]] = None,
    chroma_dir: str = CHROMA_DIR,
    collection_name: str = COLLECTION_NAME,
) -> PipelineStats:
    """
    Execute the full ETL pipeline.

    Args:
        urls:            URLs to scrape. Defaults to DEFAULT_URLS.
        chroma_dir:      Directory for persistent ChromaDB storage.
        collection_name: ChromaDB collection to upsert into.

    Returns:
        PipelineStats with counts and timing for the completed run.
    """
    if urls is None:
        urls = DEFAULT_URLS

    stats = PipelineStats()
    t_start = time.monotonic()

    logger.info("BIMA-AI vectorization pipeline starting.")
    logger.info("  ChromaDB path : %s", chroma_dir)
    logger.info("  Collection    : %s", collection_name)
    logger.info("  Embedding     : %s", EMBEDDING_MODEL)
    logger.info("  URLs to scrape: %d", len(urls))

    # --- Stage 1: Extract ---
    web_docs = _collect_web_documents(urls, stats)
    pdf_docs = _collect_pdf_documents(stats)

    all_raw_docs: list[RawDocument] = web_docs + pdf_docs
    stats.raw_docs_produced = len(all_raw_docs)

    if not all_raw_docs:
        logger.warning("No documents collected from any source. Pipeline complete (nothing to do).")
        stats.duration_seconds = time.monotonic() - t_start
        stats.log_summary()
        return stats

    # --- Stage 2: Chunk ---
    texts, metadatas, ids = _chunk_documents(all_raw_docs, stats)

    if not texts:
        logger.warning("No chunks produced after splitting. Pipeline complete (nothing to upsert).")
        stats.duration_seconds = time.monotonic() - t_start
        stats.log_summary()
        return stats

    # --- Stage 3: Embed & Upsert ---
    try:
        embeddings_model = _build_embedding_function()
        chroma_client = chromadb.PersistentClient(path=chroma_dir)
        collection = _get_or_create_collection(chroma_client)
    except Exception as exc:
        logger.critical(
            "Pipeline aborted during setup: %s", exc, exc_info=True
        )
        stats.chunks_failed = stats.chunks_created
        stats.duration_seconds = time.monotonic() - t_start
        stats.log_summary()
        return stats

    _upsert_in_batches(collection, embeddings_model, texts, metadatas, ids, stats)

    stats.duration_seconds = time.monotonic() - t_start
    stats.log_summary()
    return stats


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pipeline_stats = run_pipeline()
    # Exit with a non-zero code if every insertion failed (useful for cron alerting).
    if pipeline_stats.chunks_created > 0 and pipeline_stats.chunks_upserted == 0:
        sys.exit(1)
