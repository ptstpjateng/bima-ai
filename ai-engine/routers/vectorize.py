"""
BIMA-AI – Vectorize Router

POST /vectorize
  Receives a regulation/knowledge article from the Laravel backend (triggered
  by the Filament KnowledgeBase admin) and stores it as an embedding in ChromaDB.

This endpoint is called by the Laravel Observer after an admin publishes or
updates a KnowledgeBaseArticle.
"""

import logging
import os
from typing import Any

import chromadb
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("bima_ai.vectorize")

router = APIRouter()

_CHROMA_PATH = os.getenv("CHROMA_DB_PATH", "/app/chroma_db")
_COLLECTION_NAME = "oss_regulations"  # unified with rag_service.py


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


@router.post("/vectorize", status_code=status.HTTP_200_OK)
async def vectorize_article(body: VectorizeRequest) -> JSONResponse:
    """
    Store a regulation article as a ChromaDB embedding for RAG retrieval.
    ChromaDB's default embedding function (all-MiniLM-L6-v2) is used.
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

        metadata: dict[str, Any] = {
            "title":           body.title,
            "regulation_type": body.regulation_type,
            "region":          body.region or "",
            "kbli_codes":      ",".join(body.kbli_codes),
            "tags":            ",".join(body.tags),
            "article_id":      str(body.article_id or ""),
        }

        collection.upsert(
            ids=[body.doc_id],
            documents=[full_text],
            metadatas=[metadata],
        )

        logger.info(
            "Vectorized article | doc_id=%s | title=%s | chars=%s",
            body.doc_id,
            body.title[:60],
            len(full_text),
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
