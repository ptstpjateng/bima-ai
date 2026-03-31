"""
Pydantic data models for OSS KBLI regulatory data.

These models map the exact structure of the KBLI detail pages on oss.go.id.
They serve as the schema contract between the scraper, the JSON output,
and the RAG vectorization stage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class ScaleScope(BaseModel):
    """
    RUANG LINGKUP data for a single business scale tab.

    One instance per scale (Mikro, Kecil, Menengah, Besar).
    All detail fields are Optional because not every KBLI code lists
    every field for every scale — the OSS UI sometimes shows a dash
    or leaves the cell empty.
    """

    skala: str                              # Mikro | Kecil | Menengah | Besar
    luas_lahan: Optional[str] = None        # e.g. "Tidak Diatur", "< 500 m²"
    tingkat_risiko: Optional[str] = None    # e.g. "Rendah", "Menengah Tinggi"
    perizinan_berusaha: Optional[str] = None
    jangka_waktu: Optional[str] = None      # e.g. "Selama masih menjalankan kegiatan"
    pb_umku: Optional[str] = None           # High-level PB UMKU note from tab


class PbUmkuItem(BaseModel):
    """
    One entry from the Detailed PB UMKU section at the bottom of the page.

    The OSS portal lists specific sub-licenses (e.g. Sertifikat Laik Higiene
    Sanitasi) with optional descriptive text.
    """

    name: str
    keterangan: Optional[str] = None


class KbliDetail(BaseModel):
    """
    Complete regulatory detail record for one KBLI code.

    This is the top-level model persisted as JSON and vectorized into ChromaDB.
    """

    kbli_code: str
    detail_url: str
    uraian: str = ""
    ruang_lingkup: list[ScaleScope] = Field(default_factory=list)
    kewajiban_perizinan_berusaha: list[str] = Field(default_factory=list)
    pb_umku_detail: list[PbUmkuItem] = Field(default_factory=list)
    scraped_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    def to_text_chunks(self) -> list[tuple[str, dict]]:
        """
        Serialise this record into (text, metadata) pairs for RAG ingestion.

        Keeps each semantic unit (description, per-scale scope, obligations,
        individual licenses) in its own chunk so the retriever can pinpoint
        the right granularity at query time.
        """
        meta_base = {"kbli_code": self.kbli_code, "source_url": self.detail_url}
        chunks: list[tuple[str, dict]] = []

        # --- Chunk 1: General description ---
        if self.uraian:
            chunks.append((
                f"KBLI {self.kbli_code} – Uraian:\n{self.uraian}",
                {**meta_base, "section": "uraian"},
            ))

        # --- Chunk per business scale ---
        for scope in self.ruang_lingkup:
            lines = [f"KBLI {self.kbli_code} – Ruang Lingkup Skala {scope.skala}:"]
            if scope.luas_lahan:
                lines.append(f"Luas Lahan: {scope.luas_lahan}")
            if scope.tingkat_risiko:
                lines.append(f"Tingkat Risiko: {scope.tingkat_risiko}")
            if scope.perizinan_berusaha:
                lines.append(f"Perizinan Berusaha: {scope.perizinan_berusaha}")
            if scope.jangka_waktu:
                lines.append(f"Jangka Waktu: {scope.jangka_waktu}")
            if scope.pb_umku:
                lines.append(f"PB UMKU: {scope.pb_umku}")
            chunks.append((
                "\n".join(lines),
                {**meta_base, "section": "ruang_lingkup", "skala": scope.skala},
            ))

        # --- Chunk: Obligations ---
        if self.kewajiban_perizinan_berusaha:
            body = "\n".join(
                f"- {k}" for k in self.kewajiban_perizinan_berusaha
            )
            chunks.append((
                f"KBLI {self.kbli_code} – Kewajiban Perizinan Berusaha:\n{body}",
                {**meta_base, "section": "kewajiban"},
            ))

        # --- Chunk per PB UMKU item ---
        for item in self.pb_umku_detail:
            text = f"KBLI {self.kbli_code} – PB UMKU: {item.name}"
            if item.keterangan:
                text += f"\n{item.keterangan}"
            chunks.append((
                text,
                {**meta_base, "section": "pb_umku_detail", "item": item.name},
            ))

        return chunks


class ScrapeReport(BaseModel):
    """
    Immutable summary produced at the end of a full scraping run.

    Useful for logging, monitoring, and cron-job exit-code decisions.
    """

    kbli_codes_attempted: list[str]
    results: list[KbliDetail] = Field(default_factory=list)
    failed_codes: dict[str, str] = Field(default_factory=dict)  # code -> error msg
    run_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    @property
    def success_count(self) -> int:
        return len(self.results)

    @property
    def failure_count(self) -> int:
        return len(self.failed_codes)
