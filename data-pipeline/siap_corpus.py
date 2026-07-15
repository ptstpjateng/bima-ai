"""
BIMA data-pipeline — SIAP corpus builder (B1 persyaratan + B2 regulasi + B3 workflow/SOP).

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
import re

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


# ===========================================================================
# B2 — regulation corpus  (ptsp.tblregulasi metadata)
# ===========================================================================
# The regulation PDFs referenced by ptsp.tblregulasi.tblregulasi_file are NOT
# migrated to Beta-SIAP's storage (audited 2026-07-15: 85/86 absent), and prod
# is off-limits — so B2 ingests the authoritative *metadata* SIAP already holds
# (kategori + nomor + tentang, plus the sector often embedded in the filename).
# This is the no-hallucination guardrail against invented regulation citations;
# full PDF text is a later enhancement if/when the files become available.
#
# Default is ACTIVE regulations only (tblregulasi_status = 'T') so BIMA never
# cites a de-activated regulation as current; flip SIAP_REG_INCLUDE_INACTIVE=1
# to ingest all (each chunk still states its Aktif/Nonaktif status).

_REG_STATUS_LABELS = {"T": "Aktif", "F": "Nonaktif"}
_REG_INCLUDE_INACTIVE = os.getenv("SIAP_REG_INCLUDE_INACTIVE", "false").lower() in (
    "1", "true", "yes",
)

_REG_QUERY = """
    SELECT tblregulasi_id, tblregulasi_kategori, tblregulasi_nomor,
           tblregulasi_tentang, tblregulasi_file, tblregulasi_status
    FROM ptsp.tblregulasi
    WHERE tblregulasi_tentang IS NOT NULL AND btrim(tblregulasi_tentang) <> ''
    {status_filter}
    ORDER BY tblregulasi_id
"""


def _clean_reg_filename(fname: str) -> str:
    """Surface the sector/topic the 255-char `tentang` often truncates by
    de-noising the stored filename. Drops a Laravel hash (32 hex) / ULID
    (26-char) / date-stamp prefix and de-underscores the rest. Returns '' when
    the filename carries no human title (a bare ULID/hash), so pure-junk names
    never pollute the chunk."""
    if not fname:
        return ""
    base = fname.rsplit(".", 1)[0]
    if "_" in base:
        head, rest = base.split("_", 1)
        if (
            re.fullmatch(r"[0-9a-fA-F]{32}", head)      # md5 prefix
            or re.fullmatch(r"[0-9A-Za-z]{26}", head)   # ULID prefix
            or head.isdigit()                           # date-stamp prefix
        ):
            base = rest
    cleaned = re.sub(r"\s+", " ", re.sub(r"[_]+", " ", base)).strip()
    letters = sum(c.isalpha() for c in cleaned)
    if letters < 8 or len(cleaned.split()) < 3:
        return ""
    return cleaned


def _build_reg_chunk(row: dict) -> str:
    kat = (row.get("tblregulasi_kategori") or "").strip()
    nomor = (row.get("tblregulasi_nomor") or "").strip()
    tentang = (row.get("tblregulasi_tentang") or "").strip()
    status = _REG_STATUS_LABELS.get((row.get("tblregulasi_status") or "").strip(), "Tidak diketahui")
    header = " ".join(p for p in (kat, nomor) if p) or "Regulasi"

    lines = [f"Dasar hukum / regulasi perizinan: {header}"]
    if tentang:
        lines.append(f"Tentang: {tentang}")
    topic = _clean_reg_filename(row.get("tblregulasi_file") or "")
    if topic and topic.lower() not in tentang.lower():
        lines.append(f"Rincian judul: {topic}")
    lines.append(f"Status berlaku: {status}")
    lines.append("Sumber: registri regulasi resmi SIAP Jateng.")
    return "\n".join(lines)


def build_regulation_corpus() -> int:
    status_filter = "" if _REG_INCLUDE_INACTIVE else "AND tblregulasi_status = 'T'"
    query = _REG_QUERY.format(status_filter=status_filter)

    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        dbname=SIAP_DB,
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    log.info(
        "SIAP regulations to ingest: %d (include_inactive=%s)",
        len(rows), _REG_INCLUDE_INACTIVE,
    )
    if not rows:
        log.warning("No regulation rows returned — nothing to ingest.")
        return 0

    ids, docs, metas = [], [], []
    for row in rows:
        ids.append(f"siap-regulasi-{row['tblregulasi_id']}")
        docs.append(_build_reg_chunk(row))
        metas.append({
            "kbli_code": "",
            "section": "regulasi",
            "skala": "",
            "source_url": "SIAP Jateng - tblregulasi",
            "source": "siap_db_regulasi",
            "reg_id": int(row["tblregulasi_id"]),
            "reg_kategori": (row.get("tblregulasi_kategori") or ""),
            "reg_nomor": (row.get("tblregulasi_nomor") or ""),
            "reg_status": (row.get("tblregulasi_status") or "").strip(),
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
        log.info("reg upserted %d/%d", min(i + BATCH, len(docs)), len(docs))

    after = collection.count()
    log.info(
        "Done regulations. '%s' count %d -> %d (regulations upserted: %d)",
        COLLECTION_NAME, before, after, len(docs),
    )
    return len(docs)


if __name__ == "__main__":
    n_lic = build_siap_corpus()
    n_reg = build_regulation_corpus()
    log.info("TOTAL upserted: %d licences + %d regulations", n_lic, n_reg)
