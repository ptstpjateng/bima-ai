"""
Pydantic schemas for the KBLI admin views.

`KbliItem` is the list-row shape (subset of columns to keep payload small);
`KbliDetail` returns all 14 columns from the kblis table plus a `chunk_count`
sourced from ChromaDB (via ai-engine) for the per-code drill-down.
"""

from pydantic import BaseModel, ConfigDict


class KbliItem(BaseModel):
    """List-row shape. Excludes the long Text columns (persyaratan, kewajiban...)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kode_kbli: str
    judul_kbli: str
    sektor: str | None
    tingkat_risiko: str | None
    skala_usaha: str | None
    perizinan_berusaha: str | None
    pb_umku_names: str | None


class KbliListResponse(BaseModel):
    items: list[KbliItem]
    total: int
    page: int
    limit: int


class KbliDetail(BaseModel):
    """Full per-KBLI payload — every column from the kblis table + chunk_count."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kode_kbli: str
    judul_kbli: str
    ruang_lingkup: str | None
    skala_usaha: str | None
    tingkat_risiko: str | None
    perizinan_berusaha: str | None
    persyaratan: str | None
    jangka_waktu_penerbitan: str | None
    kewajiban: str | None
    pb_umku_names: str | None
    parameter: str | None
    kewenangan: str | None
    sektor: str | None
    # Sourced from ChromaDB via ai-engine. 0 when ai-engine is unreachable
    # (failure mode is documented inline in routers/kbli.py).
    chunk_count: int
