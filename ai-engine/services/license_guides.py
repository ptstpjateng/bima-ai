"""Curated submission guides for specific SIAP licenses.

A guide turns a license into a step-by-step submission journey: which documents
the citizen must UPLOAD, which BIMA can GENERATE for signing (via
`doc_generator`), and the extra data each generated doc needs. Phase 1 curates
PPKP (license 459) deeply; more licenses follow the same shape, or are derived
from SIAP later.
"""
from __future__ import annotations

from services import doc_generator as dg

PPKP_LICENSE_ID = 459

# Each requirement: key, label, kind ('generate'|'upload'), optional doc_type, note.
_PPKP_REQUIREMENTS: list[dict] = [
    {"key": "pakta_integritas", "label": "Pakta Integritas", "kind": "generate",
     "doc_type": dg.PAKTA_INTEGRITAS,
     "note": "BIMA buatkan drafnya — Anda tinggal tanda tangan + e-meterai."},
    {"key": "permohonan", "label": "Surat Permohonan PPKP", "kind": "generate",
     "doc_type": dg.SURAT_PERMOHONAN,
     "note": "BIMA buatkan drafnya — Anda tinggal tanda tangan + e-meterai."},
    {"key": "surat_pesanan", "label": "Surat Pesanan/Kontrak dengan galangan", "kind": "generate",
     "doc_type": dg.SURAT_PESANAN,
     "note": "BIMA buatkan draf — Anda & galangan tanda tangani + e-meterai."},
    {"key": "siup_oss", "label": "SIUP OSS (NIB/izin berusaha)", "kind": "upload"},
    {"key": "ktp", "label": "KTP Pemilik/Penanggung Jawab", "kind": "upload"},
    {"key": "gambar_kapal", "label": "Gambar rancang bangun kapal", "kind": "upload"},
    {"key": "spek_alat", "label": "Spesifikasi teknis alat penangkapan ikan", "kind": "upload"},
    {"key": "persetujuan_nama", "label": "Surat Persetujuan nama kapal (Ditjen Hubla)", "kind": "upload"},
]

# Extra data fields (beyond name/NIK/business_name BIMA already collects) the
# generated docs need — BIMA asks for these conversationally before drafting.
_PPKP_EXTRA_FIELDS: list[dict] = [
    {"key": "jabatan", "label": "Jabatan Anda (mis. Direktur/Pemilik)"},
    {"key": "alamat", "label": "Alamat perusahaan"},
    {"key": "nama_kapal", "label": "Nama kapal"},
    {"key": "gt_kapal", "label": "Ukuran kapal (GT)"},
    {"key": "bahan_kapal", "label": "Bahan kapal (mis. Baja/Kayu/Fiber)"},
    {"key": "galangan", "label": "Nama galangan pembuat kapal"},
]

_PPKP_GUIDE: dict = {
    "license_id": PPKP_LICENSE_ID,
    "name": "Persetujuan Pengadaan Kapal Perikanan (PKPP) - Pembangunan",
    "sektor": "Kelautan dan Perikanan",
    "sla_working_days": 7,
    "requirements": _PPKP_REQUIREMENTS,
    "extra_fields": _PPKP_EXTRA_FIELDS,
    "generate_docs": [r for r in _PPKP_REQUIREMENTS if r["kind"] == "generate"],
    "upload_docs": [r for r in _PPKP_REQUIREMENTS if r["kind"] == "upload"],
}

_GUIDES: dict[int, dict] = {PPKP_LICENSE_ID: _PPKP_GUIDE}


def get_guide(license_id: int | None) -> dict | None:
    """Return the curated guide for a license, or None if not curated yet."""
    if license_id is None:
        return None
    return _GUIDES.get(int(license_id))


def has_guide(license_id: int | None) -> bool:
    return get_guide(license_id) is not None
