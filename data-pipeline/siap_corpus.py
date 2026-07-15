"""
BIMA data-pipeline — SIAP corpus builder (B1 persyaratan + B3 workflow/SOP).

Reads SIAP Jateng's OWN authoritative data and upserts one rich chunk per
licence into the shared ChromaDB `oss_regulations` collection — so BIMA's RAG
answers (persyaratan + alur proses + jangka waktu) come straight from SIAP, the
source of truth, instead of a separately-maintained Excel/OSS dump. Each chunk:

    - jenis izin, kode, sektor, jangka waktu penerbitan
    - persyaratan  (ptsp.license × license_requirements × requirements)
    - alur/tahapan proses  (ptsp.license_approval_step, ordered, with unit)

The "BIMA is a layer" thesis made concrete: point BIMA at any province's SIAP
and its corpus builds itself.

Idempotent + additive: stable "siap-license-{id}" ids (re-runs UPSERT, no dup),
and it NEVER delete_collection — the KBLI/PB-UMKU chunks are preserved.

Run inside the data-pipeline container:
    python siap_corpus.py
"""
from __future__ import annotations

import logging
import os

import chromadb
import psycopg2
import psycopg2.extras
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("siap_corpus")

CHROMA_DB_PATH = os.getenv("CHROMA_DB_PATH", "/app/chroma_db")
COLLECTION_NAME = "oss_regulations"
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
SIAP_DB = os.getenv("SIAP_DB_DATABASE", "dbsiapjateng")

_EMBEDDER: SentenceTransformer | None = None


def _get_embedder() -> SentenceTransformer:
    global _EMBEDDER
    if _EMBEDDER is None:
        log.info("Loading embedding model: %s", EMBEDDING_MODEL)
        _EMBEDDER = SentenceTransformer(EMBEDDING_MODEL)
    return _EMBEDDER


# One row per licence that has persyaratan and/or a workflow. Persyaratan and
# alur are aggregated in separate CTEs so the two one-to-many joins don't
# multiply each other (Cartesian). Steps skip sort_order=0 (the PENOLAKAN
# rejection branch) and are labelled "AKSI (Unit)".
_QUERY = """
    WITH syarat AS (
        SELECT lr.license_id,
               array_agg(r.name ORDER BY lr.license_requirements_id) AS syarat
        FROM ptsp.license_requirements lr
        JOIN ptsp.requirements r ON r.requirements_id = lr.requirements_id
        WHERE r.name IS NOT NULL
        GROUP BY lr.license_id
    ),
    alur AS (
        SELECT las.license_id,
               array_agg(
                   CASE WHEN g.name IS NOT NULL AND g.name <> ''
                        THEN las.stereotype || ' (' || g.name || ')'
                        ELSE las.stereotype END
                   ORDER BY las.sort_order
               ) AS steps
        FROM ptsp.license_approval_step las
        LEFT JOIN framework.groups g ON g.group_id = las.group_id
        WHERE las.stereotype IS NOT NULL AND las.sort_order > 0
        GROUP BY las.license_id
    )
    SELECT l.license_id, l.code, l.name, l.description,
           l.properties ->> 'time_period' AS time_period,
           parent.name AS sektor,
           s.syarat, a.steps
    FROM ptsp.license l
    LEFT JOIN syarat s ON s.license_id = l.license_id
    LEFT JOIN alur a ON a.license_id = l.license_id
    LEFT JOIN ptsp.license AS parent ON l.parent_id = parent.license_id
    WHERE l.name IS NOT NULL AND (s.syarat IS NOT NULL OR a.steps IS NOT NULL)
"""


def _build_chunk(row: dict) -> str:
    lines = [f"Jenis Izin: {row['name']}"]
    if row.get("code"):
        lines.append(f"Kode Izin: {row['code']}")
    if row.get("sektor"):
        lines.append(f"Sektor/Bidang: {row['sektor']}")
    if row.get("time_period"):
        lines.append(f"Jangka waktu penerbitan: {row['time_period']} hari kerja")

    syarat = [s.strip() for s in (row.get("syarat") or []) if s and s.strip()]
    if syarat:
        lines.append(f"Persyaratan yang harus dipenuhi ({len(syarat)} syarat):")
        lines.extend(f"{i}. {s}" for i, s in enumerate(syarat, 1))

    steps = [s.strip() for s in (row.get("steps") or []) if s and s.strip()]
    if steps:
        lines.append("Alur/tahapan proses:")
        lines.extend(f"{i}. {s}" for i, s in enumerate(steps, 1))

    desc = (row.get("description") or "").strip()
    if desc:
        lines.append(f"Keterangan: {desc}")
    return "\n".join(lines)


def build_siap_corpus() -> int:
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        dbname=SIAP_DB,
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_QUERY)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    log.info("SIAP licences with persyaratan and/or workflow: %d", len(rows))
    if not rows:
        log.warning("No rows returned — nothing to ingest.")
        return 0

    ids, docs, metas = [], [], []
    for row in rows:
        ids.append(f"siap-license-{row['license_id']}")
        docs.append(_build_chunk(row))
        metas.append({
            "kbli_code": (row.get("code") or ""),
            "section": "izin",
            "skala": "",
            "source_url": "SIAP Jateng",
            "source": "siap_db",
            "license_id": int(row["license_id"]),
            "license_name": row["name"],
        })

    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
    )

    before = collection.count()
    BATCH = 64
    for i in range(0, len(docs), BATCH):
        b_docs = docs[i:i + BATCH]
        b_emb = _get_embedder().encode(
            b_docs, convert_to_numpy=True, normalize_embeddings=True
        ).tolist()
        collection.upsert(
            ids=ids[i:i + BATCH],
            documents=b_docs,
            embeddings=b_emb,
            metadatas=metas[i:i + BATCH],
        )
        log.info("upserted %d/%d", min(i + BATCH, len(docs)), len(docs))

    after = collection.count()
    log.info(
        "Done. '%s' count %d -> %d (SIAP licences upserted: %d)",
        COLLECTION_NAME, before, after, len(docs),
    )
    return len(docs)


if __name__ == "__main__":
    build_siap_corpus()
