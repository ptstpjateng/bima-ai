#!/usr/bin/env python3
"""
BIMA-AI Deterministic ETL Pipeline
===================================
Reads raw OSS Excel datasets (KBLI + PB UMKU), cleans merged-cell artifacts,
performs relational merge, generates semantic Markdown chunks, and upserts
into ChromaDB `oss_regulations` collection for RAG retrieval.

Author: BIMA-AI Data Engineering
"""

import argparse
import hashlib
import logging
import os
import re
import sys
from pathlib import Path

import chromadb
import pandas as pd
import psycopg2
from sentence_transformers import SentenceTransformer

# ── Configuration ────────────────────────────────────────────────────────────

# Defaults — preserved so direct CLI invocations and pre-existing callers
# (Filament Sync ke ChromaDB button, manual `python etl_pipeline.py`) keep
# working without flags. The Sprint C ingestion-upload feature overrides
# these via --kbli-path / --pb-umku-path.
DEFAULT_KBLI_PATH = Path("/app/data/raw_excel/kbli.xlsx")
DEFAULT_PB_UMKU_PATH = Path("/app/data/raw_excel/pb_umku.xlsx")
CHROMA_DB_PATH = "/app/chroma_db"
COLLECTION_NAME = "oss_regulations"
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

# ── Embedder singleton (BUG-002 fix) ─────────────────────────────────────────
# MUST match ai-engine/services/rag_service.py:23-32 so write-side and read-side
# share the same semantic space. ChromaDB's default (all-MiniLM-L6-v2) is
# English-only and silently corrupts Indonesian retrieval.

_EMBEDDER: SentenceTransformer | None = None


def _get_embedder() -> SentenceTransformer:
    global _EMBEDDER
    if _EMBEDDER is None:
        print(f"[etl] loading embedder {EMBEDDING_MODEL}...")
        _EMBEDDER = SentenceTransformer(EMBEDDING_MODEL)
    return _EMBEDDER

# Columns that need forward-fill due to Excel merged cells
KBLI_FFILL_COLS = ["Kode KBLI", "Judul KBLI", "Ruang Lingkup", "Sektor"]

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("etl_pipeline")

# ── Helpers ──────────────────────────────────────────────────────────────────


def clean_text(val) -> str:
    """Normalize a cell value: strip whitespace, collapse internal whitespace."""
    if pd.isna(val):
        return ""
    s = str(val).strip()
    # Fix hyphenation artifacts from Excel (e.g. "Mene-\nngah" or "Penang-kapan")
    # Pattern: lowercase letter + hyphen + optional whitespace/newline + lowercase letter
    s = re.sub(r"(\w)-\s*\n?\s*(\w)", r"\1\2", s)
    # Collapse runs of whitespace (but preserve intentional newlines for later splitting)
    s = re.sub(r"[ \t]+", " ", s)
    return s


def split_multivalue(val: str) -> list[str]:
    """Split a newline-separated cell into individual cleaned values."""
    if not val:
        return []
    parts = re.split(r"\r?\n+", val)
    return [p.strip() for p in parts if p.strip()]


def stable_id(*parts: str) -> str:
    """Generate a deterministic document ID from content parts."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.md5(raw.encode()).hexdigest()


# ── Phase 2: Load & Clean DataFrames ────────────────────────────────────────


def load_kbli(path: Path) -> pd.DataFrame:
    """Load and clean the KBLI Excel file."""
    log.info(f"Loading KBLI from {path}")
    df = pd.read_excel(path, engine="openpyxl")

    # Row 0 is a numeric sub-header (column index numbers) — drop it
    # Detect: row where 'Kode KBLI' is a small number (column index)
    first_row = df.iloc[0]
    if pd.notna(first_row["Kode KBLI"]) and isinstance(first_row["Kode KBLI"], (int, float)):
        if abs(first_row["Kode KBLI"]) < 100:  # It's a column number, not a KBLI code
            log.info("Dropping numeric sub-header row (row 0)")
            df = df.iloc[1:].reset_index(drop=True)

    # Forward-fill merged-cell columns
    for col in KBLI_FFILL_COLS:
        if col in df.columns:
            df[col] = df[col].ffill()

    # Convert Kode KBLI to 5-digit zero-padded string
    # Excel stores "03111" as float 3111.0 — we must restore the leading zero
    df["Kode KBLI"] = df["Kode KBLI"].apply(
        lambda x: str(int(x)).zfill(5) if pd.notna(x) and isinstance(x, float) else clean_text(x).zfill(5)
    )

    # Deduplicate rows caused by Excel merged-cell artifacts
    # Rows with identical (Kode KBLI, Skala Usaha, Tingkat Risiko, Perizinan Berusaha) are dupes
    dedup_cols = ["Kode KBLI", "Skala Usaha", "Tingkat Risiko", "Perizinan Berusaha"]
    before = len(df)
    df = df.drop_duplicates(subset=dedup_cols, keep="first").reset_index(drop=True)
    if before != len(df):
        log.info(f"Deduplicated: {before} → {len(df)} rows (removed {before - len(df)} dupes)")

    log.info(f"KBLI loaded: {len(df)} rows, {len(df.columns)} columns")
    log.info(f"Unique KBLI codes: {df['Kode KBLI'].nunique()}")
    return df


def load_pb_umku(path: Path) -> pd.DataFrame:
    """Load and clean the PB UMKU Excel file."""
    log.info(f"Loading PB UMKU from {path}")
    df = pd.read_excel(path, engine="openpyxl")

    # Row 0 is a numeric sub-header — drop it
    first_row = df.iloc[0]
    if isinstance(first_row.iloc[1], (int, float)):
        log.info("Dropping numeric sub-header row (row 0)")
        df = df.iloc[1:].reset_index(drop=True)

    # Drop section header rows (e.g. "I. KELAYAKAN OPERASI (B)")
    # These have non-null Nomeklatur but null Persyaratan, AND the text
    # starts with a Roman numeral pattern
    section_mask = (
        df["Nomeklatur PB UMKU"].notna()
        & df["Persyaratan"].isna()
        & df["Nomeklatur PB UMKU"].astype(str).str.match(r"^[IVX]+\.\s")
    )
    if section_mask.any():
        log.info(f"Dropping {section_mask.sum()} section header rows")
        df = df[~section_mask].reset_index(drop=True)

    # Forward-fill Nomeklatur PB UMKU for merged cells
    df["Nomeklatur PB UMKU"] = df["Nomeklatur PB UMKU"].ffill()

    # Clean Nomeklatur: remove leading asterisk annotations and trailing notes
    df["Nomeklatur PB UMKU"] = df["Nomeklatur PB UMKU"].apply(
        lambda x: re.sub(r"^\*", "", clean_text(x)).split("\n")[0].strip()
        if pd.notna(x) else ""
    )

    # Drop rows where Nomeklatur is empty after cleaning
    df = df[df["Nomeklatur PB UMKU"].str.len() > 0].reset_index(drop=True)

    log.info(f"PB UMKU loaded: {len(df)} rows")
    log.info(f"Unique PB UMKU types: {df['Nomeklatur PB UMKU'].nunique()}")
    return df


# ── Phase 3: Relational Merge ───────────────────────────────────────────────


def build_pb_umku_lookup(df_pb: pd.DataFrame) -> dict[str, list[dict]]:
    """
    Build a lookup: normalized PB UMKU name -> list of detail dicts.
    Some PB UMKU entries have multiple rows (merged cells with different persyaratan).
    We group them by Nomeklatur and aggregate.
    """
    lookup: dict[str, list[dict]] = {}
    for _, row in df_pb.iterrows():
        name = clean_text(row["Nomeklatur PB UMKU"])
        if not name:
            continue
        entry = {
            "nomeklatur": name,
            "persyaratan": clean_text(row.get("Persyaratan", "")),
            "jangka_waktu": clean_text(row.get("Jangka Waktu Penerbitan", "")),
            "kewajiban": clean_text(row.get("Kewajiban", "")),
            "masa_berlaku": clean_text(row.get("Masa Berlaku", "")),
            "parameter": clean_text(row.get("Parameter", "")),
            "kewenangan": clean_text(row.get("Kewenangan", "")),
        }
        lookup.setdefault(name, []).append(entry)
    return lookup


def fuzzy_match_pb_umku(name: str, lookup: dict[str, list[dict]]) -> list[dict]:
    """
    Match a PB UMKU name from the KBLI file against the PB UMKU lookup.
    Tries exact match first, then substring containment.
    """
    name_clean = name.strip()

    # Exact match
    if name_clean in lookup:
        return lookup[name_clean]

    # Substring match: check if lookup key contains or is contained in name
    for key, entries in lookup.items():
        if name_clean in key or key in name_clean:
            return entries

    return []


def merge_kbli_pb_umku(
    df_kbli: pd.DataFrame, pb_lookup: dict[str, list[dict]]
) -> list[dict]:
    """
    Iterate KBLI rows, expand multi-value Skala Usaha, and attach PB UMKU details.
    Returns a list of fully merged record dicts.
    """
    records = []
    total_pb_matches = 0

    for _, row in df_kbli.iterrows():
        kode = clean_text(row["Kode KBLI"])
        judul = clean_text(row["Judul KBLI"])
        ruang_lingkup = clean_text(row["Ruang Lingkup"])

        # Expand multi-value Skala Usaha (e.g., "Kecil\nMenengah\nBesar")
        skala_raw = clean_text(row["Skala Usaha"])
        skala_values = split_multivalue(skala_raw) if skala_raw else [""]

        # Handle "Menengah dan Besar" as a single scale, not split
        if len(skala_values) == 1 and " dan " in skala_values[0]:
            # Keep as-is (e.g., "Menengah dan Besar")
            pass

        # Resolve PB UMKU references
        pb_umku_names = split_multivalue(clean_text(row.get("PB UMKU", "")))
        pb_umku_details = []
        for pb_name in pb_umku_names:
            matched = fuzzy_match_pb_umku(pb_name, pb_lookup)
            if matched:
                pb_umku_details.extend(matched)
                total_pb_matches += 1
            else:
                # Still record the name even without a match
                pb_umku_details.append({"nomeklatur": pb_name, "persyaratan": "",
                                        "jangka_waktu": "", "kewajiban": "",
                                        "masa_berlaku": "", "parameter": "",
                                        "kewenangan": ""})

        for skala in skala_values:
            record = {
                "kode_kbli": kode,
                "judul_kbli": judul,
                "ruang_lingkup": ruang_lingkup,
                "skala_usaha": skala.strip(),
                "tingkat_risiko": clean_text(row.get("Tingkat Risiko", "")),
                "perizinan_berusaha": clean_text(row.get("Perizinan Berusaha", "")),
                "persyaratan": clean_text(row.get("Persyaratan", "")),
                "jangka_waktu": clean_text(row.get("Jangka Waktu Penerbitan", "")),
                "kewajiban": clean_text(row.get("Kewajiban", "")),
                "parameter": clean_text(row.get("Parameter", "")),
                "kewenangan": clean_text(row.get("Kewenangan", "")),
                "sektor": clean_text(row.get("Sektor", "")),
                "pb_umku": pb_umku_details,
            }
            records.append(record)

    log.info(f"Merge complete: {len(records)} expanded records, {total_pb_matches} PB UMKU matches")
    return records, total_pb_matches


# ── Phase 4: Semantic Chunking & ChromaDB Ingestion ─────────────────────────


def record_to_chunk(record: dict) -> str:
    """Convert a merged record into a semantic Markdown chunk for RAG."""
    parts = []

    # Header
    parts.append(
        f"## Informasi KBLI {record['kode_kbli']} — {record['judul_kbli']}"
    )

    if record["sektor"]:
        parts.append(f"**Sektor:** {record['sektor']}")

    if record["ruang_lingkup"]:
        parts.append(f"**Ruang Lingkup:** {record['ruang_lingkup']}")

    if record["skala_usaha"]:
        parts.append(f"**Skala Usaha:** {record['skala_usaha']}")

    if record["tingkat_risiko"]:
        parts.append(f"**Tingkat Risiko:** {record['tingkat_risiko']}")

    if record["perizinan_berusaha"]:
        parts.append(f"**Perizinan Berusaha:** {record['perizinan_berusaha']}")

    if record["persyaratan"]:
        parts.append(f"**Persyaratan:** {record['persyaratan']}")

    if record["jangka_waktu"]:
        parts.append(f"**Jangka Waktu Penerbitan:** {record['jangka_waktu']}")

    if record["kewajiban"]:
        parts.append(f"**Kewajiban:** {record['kewajiban']}")

    if record["parameter"]:
        parts.append(f"**Parameter:** {record['parameter']}")

    if record["kewenangan"]:
        parts.append(f"**Kewenangan:** {record['kewenangan']}")

    # PB UMKU details
    if record["pb_umku"]:
        parts.append("\n### Perizinan Berusaha Untuk Menunjang Kegiatan Usaha (PB UMKU):")
        seen = set()
        for i, pb in enumerate(record["pb_umku"], 1):
            # Deduplicate by nomeklatur
            if pb["nomeklatur"] in seen:
                continue
            seen.add(pb["nomeklatur"])

            pb_parts = [f"{i}. **{pb['nomeklatur']}**"]
            if pb["persyaratan"]:
                pb_parts.append(f"   - Persyaratan: {pb['persyaratan']}")
            if pb["jangka_waktu"]:
                pb_parts.append(f"   - Jangka Waktu: {pb['jangka_waktu']}")
            if pb["kewajiban"]:
                pb_parts.append(f"   - Kewajiban: {pb['kewajiban']}")
            if pb["masa_berlaku"]:
                pb_parts.append(f"   - Masa Berlaku: {pb['masa_berlaku']}")
            parts.append("\n".join(pb_parts))

    return "\n\n".join(parts)


def ingest_to_chromadb(records: list[dict]) -> int:
    """Upsert semantic chunks into ChromaDB with metadata for hybrid filtering."""
    log.info(f"Connecting to ChromaDB at {CHROMA_DB_PATH}")
    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)

    # Delete existing collection and recreate for clean ingestion
    try:
        client.delete_collection(COLLECTION_NAME)
        log.info(f"Deleted existing '{COLLECTION_NAME}' collection for clean re-ingestion")
    except Exception:
        pass

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    batch_ids = []
    batch_docs = []
    batch_metas = []
    BATCH_SIZE = 50
    total_inserted = 0
    seen_ids = set()

    for idx, record in enumerate(records):
        chunk = record_to_chunk(record)
        if not chunk.strip():
            continue

        doc_id = stable_id(
            record["kode_kbli"], record["skala_usaha"],
            record["tingkat_risiko"], record["perizinan_berusaha"],
            str(idx), "excel_etl"
        )
        # Guarantee uniqueness
        if doc_id in seen_ids:
            doc_id = stable_id(doc_id, str(idx), "dedup")
        seen_ids.add(doc_id)

        metadata = {
            "kode_kbli": record["kode_kbli"],
            "judul_kbli": record["judul_kbli"][:200],  # ChromaDB metadata size limit
            "skala_usaha": record["skala_usaha"],
            "tingkat_risiko": record["tingkat_risiko"],
            "sektor": record["sektor"],
            "source": "excel_etl",
            "has_pb_umku": len(record["pb_umku"]) > 0,
        }

        batch_ids.append(doc_id)
        batch_docs.append(chunk)
        batch_metas.append(metadata)

        if len(batch_ids) >= BATCH_SIZE:
            embeddings = _get_embedder().encode(batch_docs, convert_to_numpy=True).tolist()
            collection.upsert(
                ids=batch_ids,
                documents=batch_docs,
                embeddings=embeddings,
                metadatas=batch_metas,
            )
            total_inserted += len(batch_ids)
            batch_ids, batch_docs, batch_metas = [], [], []

    # Flush remaining
    if batch_ids:
        embeddings = _get_embedder().encode(batch_docs, convert_to_numpy=True).tolist()
        collection.upsert(
            ids=batch_ids,
            documents=batch_docs,
            embeddings=embeddings,
            metadatas=batch_metas,
        )
        total_inserted += len(batch_ids)

    log.info(f"ChromaDB ingestion complete: {total_inserted} chunks upserted")
    return total_inserted


# ── Phase 4b: PostgreSQL Sync ───────────────────────────────────────────────
#
# Bug-Fix (May 2026): the ETL previously wrote ChromaDB only, so the AI bot
# answered correctly via RAG but admin-api's /kbli browser + dashboard
# active_kbli_codes stat (which read from PostgreSQL `kblis` / `pb_umkus`)
# silently lagged behind. We now mirror the freshly-built records back into
# Postgres in the same run, wrapped in a single transaction so a mid-write
# failure leaves the prior state intact.
#
# These tables are Laravel-owned (no Alembic migration here) and have no
# incoming FKs — verified on prod via `pg_constraint` lookup — so TRUNCATE
# ... RESTART IDENTITY is safe.


# Laravel migrated `kblis` with VARCHAR(255) on several columns. The original
# 94-code dataset never hit that limit, but a real KBLI export hits it on a
# minority of `judul_kbli` / `skala_usaha` / etc. Truncate before insert so
# the admin browser shows a (clipped) row instead of the whole sync failing.
# ChromaDB still has the full text — the truncation is cosmetic for browsing.
_VARCHAR_MAX = 250  # 5-char safety margin below the column cap

# Columns the Laravel migration declared as VARCHAR(255), per
# admin-api/app/models/kbli.py (model is reflective, not authoritative —
# kept in sync with the live DB).
_KBLI_VARCHAR_COLS = {
    "judul_kbli", "skala_usaha", "tingkat_risiko",
    "jangka_waktu", "kewenangan", "sektor",
}


def _clip(value: object, n: int = _VARCHAR_MAX) -> object:
    """Truncate a string to n chars; leave non-strings/None alone."""
    if isinstance(value, str) and len(value) > n:
        return value[:n]
    return value


def _records_to_kbli_rows(records: list[dict]) -> list[tuple]:
    """
    Collapse the per-skala-usaha expanded records back to one row per
    (kode_kbli, skala_usaha, tingkat_risiko, perizinan_berusaha) — the
    Laravel `kblis` table is a flat denormalised mirror.

    String columns declared VARCHAR(255) at the DB layer are truncated to
    250 chars; TEXT columns are passed through whole. pb_umku_names is a
    newline-joined list of nomeklatur for the record (Laravel importer
    convention).
    """
    rows: list[tuple] = []
    seen: set[tuple] = set()
    for r in records:
        key = (r["kode_kbli"], r["skala_usaha"], r["tingkat_risiko"], r["perizinan_berusaha"])
        if key in seen:
            continue
        seen.add(key)
        pb_names = "\n".join(
            sorted({pb["nomeklatur"] for pb in r["pb_umku"] if pb.get("nomeklatur")})
        )
        rows.append(
            (
                r["kode_kbli"],                          # VARCHAR(10) — kbli codes are 5 digits, safe
                _clip(r["judul_kbli"]),                  # VARCHAR(255)
                r["ruang_lingkup"] or None,              # TEXT
                _clip(r["skala_usaha"]) or None,         # VARCHAR(255)
                _clip(r["tingkat_risiko"]) or None,      # VARCHAR(255)
                r["perizinan_berusaha"] or None,         # TEXT
                r["persyaratan"] or None,                # TEXT
                _clip(r["jangka_waktu"]) or None,        # VARCHAR(255)
                r["kewajiban"] or None,                  # TEXT
                pb_names or None,                        # TEXT
                r["parameter"] or None,                  # TEXT
                _clip(r["kewenangan"]) or None,          # VARCHAR(255)
                _clip(r["sektor"]) or None,              # VARCHAR(255)
            )
        )
    return rows


def _lookup_to_pb_umku_rows(pb_lookup: dict[str, list[dict]]) -> list[tuple]:
    """
    Flatten the PB UMKU lookup into rows for the `pb_umkus` table.
    Same column order as live DB (verified May 2026): nomeklatur, persyaratan,
    jangka_waktu_penerbitan, kewajiban, masa_berlaku, parameter, kewenangan.
    Note the Excel typo `nomeklatur` (not `nomenklatur`) — preserved in the DB.

    Defensive truncation to 250 chars on every column — we don't know which
    are VARCHAR vs TEXT in the Laravel migration; clipping is safe either way.
    """
    rows: list[tuple] = []
    seen: set[tuple] = set()
    for entries in pb_lookup.values():
        for entry in entries:
            key = (
                entry["nomeklatur"],
                entry["persyaratan"],
                entry["jangka_waktu"],
                entry["kewajiban"],
                entry["masa_berlaku"],
                entry["parameter"],
                entry["kewenangan"],
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                (
                    _clip(entry["nomeklatur"]),
                    _clip(entry["persyaratan"]) or None,
                    _clip(entry["jangka_waktu"]) or None,
                    _clip(entry["kewajiban"]) or None,
                    _clip(entry["masa_berlaku"]) or None,
                    _clip(entry["parameter"]) or None,
                    _clip(entry["kewenangan"]) or None,
                )
            )
    return rows


def sync_to_postgres(
    records: list[dict], pb_lookup: dict[str, list[dict]]
) -> tuple[int, int]:
    """
    Truncate `kblis` + `pb_umkus` and re-insert the freshly-built records in a
    single transaction. Returns (n_kbli, n_pb).

    Env vars (from data-pipeline/.env, see BIMA/CLAUDE.md § Data Pipeline env):
      DB_HOST, DB_PORT (default 5432), DB_DATABASE, DB_USERNAME, DB_PASSWORD.

    The two TRUNCATEs share the same `with conn:` block, so either both tables
    flip to the new content or neither does — psycopg2 commits on context exit
    and rolls back on any exception. There are no incoming FKs to either
    table (verified via pg_constraint), so the plain TRUNCATE form is safe.
    """
    kbli_rows = _records_to_kbli_rows(records)
    pb_rows = _lookup_to_pb_umku_rows(pb_lookup)

    log.info(
        f"PostgreSQL sync: prepared {len(kbli_rows)} kbli rows, {len(pb_rows)} pb_umku rows"
    )

    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_DATABASE"],
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE kblis RESTART IDENTITY")
                cur.executemany(
                    """
                    INSERT INTO kblis (
                        kode_kbli, judul_kbli, ruang_lingkup, skala_usaha,
                        tingkat_risiko, perizinan_berusaha, persyaratan,
                        jangka_waktu_penerbitan, kewajiban, pb_umku_names,
                        parameter, kewenangan, sektor,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        NOW(), NOW()
                    )
                    """,
                    kbli_rows,
                )

                cur.execute("TRUNCATE pb_umkus RESTART IDENTITY")
                cur.executemany(
                    """
                    INSERT INTO pb_umkus (
                        nomeklatur, persyaratan, jangka_waktu_penerbitan,
                        kewajiban, masa_berlaku, parameter, kewenangan,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        NOW(), NOW()
                    )
                    """,
                    pb_rows,
                )
        log.info(
            f"PostgreSQL kblis + pb_umkus synced: {len(kbli_rows)} kblis / {len(pb_rows)} pb_umkus"
        )
        return len(kbli_rows), len(pb_rows)
    finally:
        conn.close()


# ── Phase 5: Main Execution ─────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    """
    Accept optional per-file path overrides. Defaults preserve legacy
    behaviour for any non-Sprint-C caller.
    """
    parser = argparse.ArgumentParser(description="BIMA-AI deterministic ETL pipeline.")
    parser.add_argument(
        "--kbli-path",
        type=Path,
        default=DEFAULT_KBLI_PATH,
        help="Override KBLI .xlsx path (default: /app/data/raw_excel/kbli.xlsx).",
    )
    parser.add_argument(
        "--pb-umku-path",
        type=Path,
        default=DEFAULT_PB_UMKU_PATH,
        help="Override PB UMKU .xlsx path (default: /app/data/raw_excel/pb_umku.xlsx).",
    )
    return parser.parse_args()


def main(kbli_path: Path | None = None, pb_umku_path: Path | None = None):
    kbli_path = kbli_path or DEFAULT_KBLI_PATH
    pb_umku_path = pb_umku_path or DEFAULT_PB_UMKU_PATH

    log.info("=" * 60)
    log.info("BIMA-AI Deterministic ETL Pipeline — Starting")
    log.info(f"  KBLI source:    {kbli_path}")
    log.info(f"  PB UMKU source: {pb_umku_path}")
    log.info("=" * 60)

    # Validate input files
    for path in [kbli_path, pb_umku_path]:
        if not path.exists():
            log.error(f"Input file not found: {path}")
            sys.exit(1)

    # Phase 2: Load & Clean
    try:
        df_kbli = load_kbli(kbli_path)
        df_pb_umku = load_pb_umku(pb_umku_path)
    except Exception as e:
        log.error(f"Failed to load Excel files: {e}")
        sys.exit(1)

    # Phase 3: Build lookup & merge
    try:
        pb_lookup = build_pb_umku_lookup(df_pb_umku)
        log.info(f"PB UMKU lookup built: {len(pb_lookup)} unique entries")
        records, pb_match_count = merge_kbli_pb_umku(df_kbli, pb_lookup)
    except Exception as e:
        log.error(f"Failed during merge: {e}")
        sys.exit(1)

    # Phase 4: Chunk & Ingest
    try:
        total_chunks = ingest_to_chromadb(records)
    except Exception as e:
        log.error(f"Failed during ChromaDB ingestion: {e}")
        sys.exit(1)

    # Phase 4b: Mirror to PostgreSQL. Best-effort — if this fails the RAG
    # update is still live, so the bot benefits even if the admin browser
    # doesn't refresh. We log loudly and keep going.
    pg_kbli_count = 0
    pg_pb_count = 0
    try:
        pg_kbli_count, pg_pb_count = sync_to_postgres(records, pb_lookup)
    except Exception as e:
        log.error(
            f"PostgreSQL sync failed (ChromaDB already updated; admin browser "
            f"will be stale until next successful run): {e}"
        )

    # Summary
    log.info("=" * 60)
    log.info("ETL PIPELINE SUMMARY")
    log.info("=" * 60)
    log.info(f"  KBLI rows loaded (raw):         {len(df_kbli)}")
    log.info(f"  Unique KBLI codes:               {df_kbli['Kode KBLI'].nunique()}")
    log.info(f"  PB UMKU entries loaded:          {len(df_pb_umku)}")
    log.info(f"  PB UMKU lookup entries:          {len(pb_lookup)}")
    log.info(f"  Expanded records (after merge):  {len(records)}")
    log.info(f"  PB UMKU matches found:           {pb_match_count}")
    log.info(f"  Chunks inserted into ChromaDB:   {total_chunks}")
    log.info(f"  PostgreSQL kblis rows:           {pg_kbli_count}")
    log.info(f"  PostgreSQL pb_umkus rows:        {pg_pb_count}")
    log.info(f"  Collection: '{COLLECTION_NAME}'")
    log.info("=" * 60)
    # Machine-readable beacon parsed by server.py to populate the ETL state's
    # `chunks_indexed` field. Format is part of the contract — do not change
    # without updating server.py's regex.
    print(f"[etl] indexed {total_chunks} chunks", flush=True)
    # Companion beacon for postgres sync — same convention so admin-api can
    # parse for stats later if it wants. Only emitted when the sync succeeded.
    if pg_kbli_count or pg_pb_count:
        print(
            f"[etl] postgres synced {pg_kbli_count} kblis / {pg_pb_count} pb_umkus",
            flush=True,
        )
    log.info("Pipeline completed successfully.")


if __name__ == "__main__":
    # Startup self-test: fail fast if the embedder cannot load, rather than
    # silently falling back to ChromaDB's default model mid-ingest.
    try:
        _get_embedder()
    except Exception as e:
        print(f"[etl] FATAL: embedder failed to load: {e}", file=sys.stderr)
        sys.exit(1)

    args = _parse_args()
    main(kbli_path=args.kbli_path, pb_umku_path=args.pb_umku_path)
