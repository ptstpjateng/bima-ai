"""Curated submission guides for specific SIAP licenses.

A guide turns a license into a step-by-step submission journey: which documents
the citizen must UPLOAD, which BIMA can GENERATE for signing (via
`doc_generator`), and the extra data each generated doc needs. Phase 1 curates
PPKP (license 459) deeply; more licenses follow the same shape, or are derived
from SIAP later.
"""
from __future__ import annotations

# doc_type values are the string constants from services.doc_generator
# (PAKTA_INTEGRITAS / SURAT_PERMOHONAN / SURAT_PESANAN). We hardcode the strings
# rather than importing doc_generator so this module stays free of the fpdf2
# dependency — importable in the test harness (and at _lock_license time)
# without fpdf2 installed. test_doc_generator asserts they match DOC_TYPES.
PPKP_LICENSE_ID = 459

# Each requirement: key, label, kind ('generate'|'upload'), optional doc_type, note.
#
# `where` — WHO ISSUES an upload doc, for when the citizen asks "dimana saya bisa
# dapat ...". It is OPTIONAL and deliberately sparse. The rule that keeps this
# honest: **only name an issuer this guide already vouches for.** Every `where`
# below restates an authority the `label` right next to it already asserts
# (persetujuan_nama says "Ditjen Hubla"; siup_oss says "OSS") — so it is curated
# fact being restructured, never a new claim.
#
# The omissions are the point. A ship's design drawing and the gear spec plainly
# come from the galangan, but nothing here vouches for that, so they carry no
# `where` and BIMA says it does not know and offers the officer instead. This is
# a citizen being told where to obtain a government document: a plausible-sounding
# wrong office sends a real person to the wrong building. An honest "saya belum
# punya info itu" costs them one question to a human; a confident wrong answer
# costs them a trip. Do NOT fill these in from general knowledge — add a `where`
# only when someone who owns the process has confirmed it.
_PPKP_REQUIREMENTS: list[dict] = [
    {"key": "pakta_integritas", "label": "Pakta Integritas", "kind": "generate",
     "doc_type": "pakta_integritas",
     "note": "BIMA buatkan drafnya — Anda tinggal tanda tangan + e-meterai."},
    {"key": "permohonan", "label": "Surat Permohonan PPKP", "kind": "generate",
     "doc_type": "surat_permohonan",
     "note": "BIMA buatkan drafnya — Anda tinggal tanda tangan + e-meterai."},
    {"key": "surat_pesanan", "label": "Surat Pesanan/Kontrak dengan galangan", "kind": "generate",
     "doc_type": "surat_pesanan",
     "note": "BIMA buatkan draf — Anda & galangan tanda tangani + e-meterai."},
    {"key": "siup_oss", "label": "SIUP OSS (NIB/izin berusaha)", "kind": "upload",
     "where": "sistem OSS (yang menerbitkan NIB/izin berusaha Anda)"},
    {"key": "ktp", "label": "KTP Pemilik/Penanggung Jawab", "kind": "upload"},
    {"key": "gambar_kapal", "label": "Gambar rancang bangun kapal", "kind": "upload"},
    {"key": "spek_alat", "label": "Spesifikasi teknis alat penangkapan ikan", "kind": "upload"},
    {"key": "persetujuan_nama", "label": "Surat Persetujuan nama kapal (Ditjen Hubla)", "kind": "upload",
     "where": "Ditjen Perhubungan Laut (Hubla)"},
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
