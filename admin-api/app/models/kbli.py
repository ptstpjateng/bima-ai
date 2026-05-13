"""
`kblis` — Laravel-owned (truncated and refilled by the Excel ingest pipeline).
Pure struct-of-strings; no relationships per backend-laravel.md.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Kbli(Base):
    __tablename__ = "kblis"
    __table_args__ = {"keep_existing": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kode_kbli: Mapped[str] = mapped_column(String(10), index=True)
    judul_kbli: Mapped[str] = mapped_column(String(255))
    ruang_lingkup: Mapped[str | None] = mapped_column(Text, nullable=True)
    skala_usaha: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tingkat_risiko: Mapped[str | None] = mapped_column(String(255), nullable=True)
    perizinan_berusaha: Mapped[str | None] = mapped_column(Text, nullable=True)
    persyaratan: Mapped[str | None] = mapped_column(Text, nullable=True)
    jangka_waktu_penerbitan: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kewajiban: Mapped[str | None] = mapped_column(Text, nullable=True)
    pb_umku_names: Mapped[str | None] = mapped_column(Text, nullable=True)
    parameter: Mapped[str | None] = mapped_column(Text, nullable=True)
    kewenangan: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sektor: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
