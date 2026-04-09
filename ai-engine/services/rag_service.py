"""
BIMA-AI – RAG Service

Queries ChromaDB for relevant OSS RBA regulations based on the user's message.
Returns ranked text chunks to inject into the LLM prompt as context.
"""

import logging
import os

import chromadb

logger = logging.getLogger("bima_ai.rag")

_CHROMA_PATH = os.getenv("CHROMA_DB_PATH", "/app/chroma_db")
_COLLECTION_NAME = "oss_regulations"
_DEFAULT_N_RESULTS = 4

# BUG-002 FIX — load the same embedding model used at ingest time so query
# vectors live in the same semantic space as the stored document vectors.
# The data-pipeline embedded with paraphrase-multilingual-MiniLM-L12-v2;
# using ChromaDB's default (all-MiniLM-L6-v2) here caused wrong-KBLI results.
_EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_embedder = None  # loaded lazily on first query


def _get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model: %s", _EMBED_MODEL_NAME)
        _embedder = SentenceTransformer(_EMBED_MODEL_NAME)
    return _embedder


def query_regulations(query_text: str, n_results: int = _DEFAULT_N_RESULTS) -> list[dict]:
    """
    Query ChromaDB for the most semantically relevant regulation chunks.

    Returns a list of dicts with keys: content, kbli_code, section, skala,
    source_url, distance.
    Returns [] if the collection is empty or ChromaDB is unavailable.
    """
    try:
        client = chromadb.PersistentClient(path=_CHROMA_PATH)
        collection = client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

        count = collection.count()
        if count == 0:
            logger.info("ChromaDB collection is empty — no RAG context available.")
            return []

        # BUG-002 FIX — pre-embed query with the multilingual model, then pass
        # as query_embeddings= so ChromaDB does NOT re-embed with its default.
        embedder = _get_embedder()
        query_embedding = embedder.encode(query_text, normalize_embeddings=True).tolist()

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(n_results, count),
            include=["documents", "metadatas", "distances"],
        )

        chunks: list[dict] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        for doc, meta, dist in zip(docs, metas, dists):
            # BUG-001 FIX — map the actual ChromaDB metadata keys produced by the
            # data-pipeline (kbli_code, section, skala, source_url) instead of the
            # old wrong keys (title, regulation_type, region) that always resolved
            # to empty strings and stripped all source attribution from the LLM prompt.
            # Also handles 6 legacy docs stored with the old keys (kbli, type).
            chunks.append({
                "content":    doc,
                "kbli_code":  meta.get("kbli_code") or meta.get("kbli", ""),
                "section":    meta.get("section") or meta.get("type", ""),
                "skala":      meta.get("skala", ""),
                "source_url": meta.get("source_url", ""),
                "distance":   dist,
            })

        logger.info(
            "RAG query returned %d chunks | query_len=%d | top_dist=%.3f",
            len(chunks),
            len(query_text),
            dists[0] if dists else 0,
        )
        return chunks

    except Exception:
        logger.exception("RAG query failed — continuing without context.")
        return []


def format_rag_context(chunks: list[dict]) -> str:
    """
    Format RAG chunks into a concise context block for LLM injection.
    Only includes chunks with cosine distance < 0.7 (reasonably relevant).
    """
    relevant = [c for c in chunks if c.get("distance", 1.0) < 0.7]
    if not relevant:
        return ""

    lines = ["=== KONTEKS REGULASI OSS RBA ==="]
    for i, chunk in enumerate(relevant, 1):
        kbli = chunk.get("kbli_code") or "—"
        section = chunk.get("section") or "umum"
        skala = chunk.get("skala", "")
        # BUG-001 FIX — produce a meaningful attribution label so the LLM knows
        # exactly which KBLI and scale the chunk belongs to.
        label = f"KBLI {kbli} ({section}" + (f" — {skala}" if skala else "") + ")"
        lines.append(
            f"\n[{i}] {label}\n"
            f"{chunk['content'][:800]}"  # Truncate long chunks.
        )
    lines.append("\n=== AKHIR KONTEKS ===")
    return "\n".join(lines)
