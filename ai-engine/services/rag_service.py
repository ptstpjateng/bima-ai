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
_COLLECTION_NAME = "bima_regulations"
_DEFAULT_N_RESULTS = 4


def query_regulations(query_text: str, n_results: int = _DEFAULT_N_RESULTS) -> list[dict]:
    """
    Query ChromaDB for the most semantically relevant regulation chunks.

    Returns a list of dicts with keys: content, title, regulation_type, region, distance.
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

        results = collection.query(
            query_texts=[query_text],
            n_results=min(n_results, count),
            include=["documents", "metadatas", "distances"],
        )

        chunks: list[dict] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        for doc, meta, dist in zip(docs, metas, dists):
            chunks.append({
                "content":         doc,
                "title":           meta.get("title", ""),
                "regulation_type": meta.get("regulation_type", ""),
                "region":          meta.get("region", ""),
                "distance":        dist,
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
        lines.append(
            f"\n[{i}] {chunk['title']} ({chunk['regulation_type']}, {chunk['region'] or 'Nasional'})\n"
            f"{chunk['content'][:800]}"  # Truncate long chunks.
        )
    lines.append("\n=== AKHIR KONTEKS ===")
    return "\n".join(lines)
