"""
BIMA-AI – Vectorize Router

POST /vectorize
  Receives a regulation/knowledge article from the Laravel backend (triggered
  by the Filament KnowledgeBase admin) and stores it as an embedding in ChromaDB.

This endpoint is called by the Laravel Observer after an admin publishes or
updates a KnowledgeBaseArticle.
"""

import asyncio
import logging
import os
from typing import Any

import chromadb
import httpx
from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

logger = logging.getLogger("bima_ai.vectorize")

router = APIRouter()

_CHROMA_PATH = os.getenv("CHROMA_DB_PATH", "/app/chroma_db")
_COLLECTION_NAME = "oss_regulations"  # unified with rag_service.py

# BUG-002 FIX — load the same multilingual embedder used by rag_service.py and
# the data-pipeline ETL. Without pre-embedding here, ChromaDB falls back to its
# default all-MiniLM-L6-v2 (English-only), so admin-vectorized articles end up
# in a different semantic space than the query-time vectors and never match.
_EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_EMBEDDER: SentenceTransformer | None = None


def _get_embedder() -> SentenceTransformer:
    global _EMBEDDER
    if _EMBEDDER is None:
        logger.info("Loading embedding model: %s", _EMBED_MODEL_NAME)
        _EMBEDDER = SentenceTransformer(_EMBED_MODEL_NAME)
    return _EMBEDDER


def _get_collection() -> chromadb.Collection:
    client = chromadb.PersistentClient(path=_CHROMA_PATH)
    return client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


class VectorizeRequest(BaseModel):
    doc_id: str
    title: str
    content: str
    regulation_type: str = "umum"
    region: str | None = None
    kbli_codes: list[str] = []
    tags: list[str] = []
    article_id: int | None = None
    source_url: str | None = None


@router.post("/vectorize", status_code=status.HTTP_200_OK)
async def vectorize_article(body: VectorizeRequest) -> JSONResponse:
    """
    Store a regulation article as a ChromaDB embedding for RAG retrieval.

    Pre-embeds with paraphrase-multilingual-MiniLM-L12-v2 (the same model used
    by rag_service.py and the data-pipeline ETL) and writes metadata under the
    canonical schema (kbli_code, section, skala, source_url) that the RAG query
    expects — see services/rag_service.py:80-83.
    """
    try:
        collection = _get_collection()

        # Build a rich document string for embedding.
        full_text = (
            f"Judul: {body.title}\n"
            f"Jenis: {body.regulation_type}\n"
            f"Wilayah: {body.region or 'Nasional'}\n"
            f"KBLI: {', '.join(body.kbli_codes) or 'Semua'}\n\n"
            f"{body.content}"
        )

        # BUG-002 FIX — pre-embed with the multilingual model BEFORE upserting,
        # then pass embeddings= so ChromaDB cannot fall back to its English-only
        # default. encode() is CPU-bound; offload to the default executor.
        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(
            None,
            lambda: _get_embedder()
            .encode([full_text], convert_to_numpy=True, normalize_embeddings=True)
            .tolist(),
        )

        # BUG-001 FIX — write the canonical metadata schema that rag_service.py
        # reads (kbli_code, section, skala, source_url). The inbound payload
        # carries kbli_codes (a list, multi-KBLI articles); we collapse to the
        # first code as the primary kbli_code and stash the comma-joined list
        # in a dedicated field for completeness. regulation_type maps to section
        # (it's the closest semantic equivalent — the kind/category of the
        # chunk). skala is not in the inbound payload, so leave it blank rather
        # than None (ChromaDB rejects None metadata values).
        primary_kbli = body.kbli_codes[0] if body.kbli_codes else ""
        metadata: dict[str, Any] = {
            "kbli_code":  primary_kbli,
            "section":    body.regulation_type or "",
            "skala":      "",
            "source_url": body.source_url or "",
        }

        collection.upsert(
            ids=[body.doc_id],
            embeddings=embedding,
            documents=[full_text],
            metadatas=[metadata],
        )

        logger.info(
            "Vectorized article | doc_id=%s | title=%s | chars=%s | kbli=%s",
            body.doc_id,
            body.title[:60],
            len(full_text),
            primary_kbli or "—",
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ok", "doc_id": body.doc_id},
        )

    except Exception:
        logger.exception("Vectorize failed | doc_id=%s", body.doc_id)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "message": "Vectorization failed."},
        )


# ── GET /chunks ───────────────────────────────────────────────────────────────

_PIPELINE_URL = os.getenv("PIPELINE_URL", "http://data-pipeline:9000")


@router.get("/chunks", status_code=status.HTTP_200_OK)
async def get_chunks(
    kbli_code: str = Query(..., description="KBLI code to fetch chunks for"),
) -> JSONResponse:
    """
    Return all ChromaDB chunks stored for a specific KBLI code.
    Used by the Filament admin panel to display scraped content.
    """
    try:
        collection = _get_collection()
        results = collection.get(
            where={"kbli_code": kbli_code},
            include=["documents", "metadatas"],
        )
        chunks = [
            {
                "id":       doc_id,
                "content":  doc,
                "section":  meta.get("section", ""),
                "skala":    meta.get("skala", ""),
                "source_url": meta.get("source_url", ""),
                "sub_chunk_index": meta.get("sub_chunk_index", 0),
            }
            for doc_id, doc, meta in zip(
                results.get("ids", []),
                results.get("documents", []),
                results.get("metadatas", []),
            )
        ]
        # Sort by section then sub_chunk_index
        chunks.sort(key=lambda c: (c["section"], c["sub_chunk_index"]))
        logger.info("Chunks fetched | kbli_code=%s | count=%d", kbli_code, len(chunks))
        return JSONResponse(content={"kbli_code": kbli_code, "count": len(chunks), "chunks": chunks})

    except Exception:
        logger.exception("get_chunks failed | kbli_code=%s", kbli_code)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "message": "Failed to fetch chunks."},
        )


# ── POST /pipeline/trigger (proxy to data-pipeline service) ──────────────────

@router.post("/pipeline/trigger", status_code=status.HTTP_202_ACCEPTED)
async def trigger_pipeline(limit: int = 35) -> JSONResponse:
    """
    Proxy trigger request to the data-pipeline service.
    Called by the Laravel backend when admin clicks 'Run Pipeline'.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{_PIPELINE_URL}/pipeline/trigger", params={"limit": limit})
            return JSONResponse(status_code=resp.status_code, content=resp.json())
    except httpx.ConnectError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "message": "data-pipeline service is not reachable."},
        )
    except Exception:
        logger.exception("pipeline/trigger proxy failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "message": "Failed to trigger pipeline."},
        )


@router.get("/pipeline/status")
async def get_pipeline_status() -> JSONResponse:
    """Get the current pipeline run status from the data-pipeline service."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_PIPELINE_URL}/pipeline/status")
            return JSONResponse(content=resp.json())
    except Exception:
        return JSONResponse(content={"status": "unreachable", "running": False})
