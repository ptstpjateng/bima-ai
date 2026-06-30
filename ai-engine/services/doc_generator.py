"""Generate ready-to-sign Indonesian licensing documents (PDF) from citizen data.

Phase 1 of the PPKP guided-submission copilot. BIMA fills the three documents an
applicant must create + sign — **Pakta Integritas**, **Surat Permohonan**, and
**Surat Pesanan** — with the data it already collected (nama, NIK, perusahaan,
data kapal), leaving a signature + e-meterai block for the citizen to complete
via Mekari (e-sign + e-meterai). The citizen reviews, signs, and uploads the
signed PDF; the rest of the requirements they upload directly.

Pure-Python (fpdf2) — no system libraries, so it adds cleanly to the container.

Public API:
    generate(doc_type, data) -> bytes      # doc_type in DOC_TYPES
    DOC_TYPES                              # the 3 generatable docs + labels
    required_fields(doc_type) -> list[str] # which data keys each doc wants
"""
from __future__ import annotations

import io
import logging
import secrets
from datetime import datetime, timezone, timedelta

from fpdf import FPDF
from fpdf.enums import EncryptionMethod

logger = logging.getLogger("bima_ai.doc_generator")

# Doc types BIMA can generate-and-hand-over for signing.
PAKTA_INTEGRITAS = "pakta_integritas"
SURAT_PERMOHONAN = "surat_permohonan"
SURAT_PESANAN = "surat_pesanan"

DOC_TYPES: dict[str, str] = {
    PAKTA_INTEGRITAS: "Pakta Integritas",
    SURAT_PERMOHONAN: "Surat Permohonan",
    SURAT_PESANAN: "Surat Pesanan",
}

_PLACE = "Semarang"
_MISSING = "________________"
_WIB = timezone(timedelta(hours=7))

# fpdf2 core fonts (Helvetica) are latin-1 only. Map the few non-latin-1
# characters we, or the citizen's data, might carry to safe equivalents, then
# hard-replace anything left so PDF generation never crashes on an exotic glyph.
_SUBS = {
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", " ": " ",
    "•": "-", "×": "x",
}


def _safe(s) -> str:
    out = str(s)
    for k, v in _SUBS.items():
        out = out.replace(k, v)
    return out.encode("latin-1", "replace").decode("latin-1")

# Data keys each document fills. Missing keys render as a blank line the citizen
# can complete by hand before signing — never invented.
_FIELDS: dict[str, list[str]] = {
    PAKTA_INTEGRITAS: ["applicant_name", "nik", "jabatan", "business_name", "alamat", "license_name"],
    SURAT_PERMOHONAN: ["applicant_name", "jabatan", "business_name", "alamat",
                       "license_name", "nama_kapal", "gt_kapal"],
    SURAT_PESANAN:    ["applicant_name", "jabatan", "business_name", "galangan",
                       "nama_kapal", "gt_kapal", "bahan_kapal"],
}


def required_fields(doc_type: str) -> list[str]:
    return list(_FIELDS.get(doc_type, []))


def _today_id() -> str:
    """'12 Juni 2026' in WIB. (Runtime datetime — fine in regular Python.)"""
    bulan = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
             "Agustus", "September", "Oktober", "November", "Desember"]
    now = datetime.now(_WIB)
    return f"{now.day} {bulan[now.month - 1]} {now.year}"


def _v(data: dict, key: str) -> str:
    val = data.get(key)
    return str(val).strip() if val not in (None, "") else _MISSING


class _Doc(FPDF):
    # Sanitize every string to latin-1 (core-font safe) at the single choke point.
    def cell(self, w=0, h=0, text="", *args, **kwargs):
        return super().cell(w, h, _safe(text), *args, **kwargs)

    def multi_cell(self, w=0, h=0, text="", *args, **kwargs):
        return super().multi_cell(w, h, _safe(text), *args, **kwargs)

    def footer(self) -> None:
        self.set_y(-16)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(150)
        self.multi_cell(
            0, 4,
            "Draf dibuat dengan bantuan BIMA-AI — DPMPTSP Provinsi Jawa Tengah. "
            "Tanda tangani dan bubuhi meterai Rp10.000 sebelum diunggah — "
            "e-meterai (mis. Mekari) atau meterai tempel, keduanya sah.",
            align="C",
        )
        self.set_text_color(0)


def _new() -> _Doc:
    pdf = _Doc(format="A4")
    pdf.set_auto_page_break(auto=True, margin=22)
    pdf.add_page()
    pdf.set_margins(25, 22, 25)
    return pdf


def _title(pdf: _Doc, text: str, subtitle: str = "") -> None:
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 9, text, align="C", new_x="LMARGIN", new_y="NEXT")
    if subtitle:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, subtitle, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    # underline
    y = pdf.get_y()
    pdf.set_draw_color(180)
    pdf.line(60, y, pdf.w - 60, y)
    pdf.set_draw_color(0)
    pdf.ln(6)


def _kv(pdf: _Doc, label: str, value: str) -> None:
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(48, 7, label, new_x="RIGHT", new_y="TOP")
    pdf.cell(5, 7, ":", new_x="RIGHT", new_y="TOP")
    pdf.set_font("Helvetica", "B", 11)
    pdf.multi_cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")


def _para(pdf: _Doc, text: str) -> None:
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(0, 6.5, text, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def _signature_block(pdf: _Doc, name: str, role: str = "Yang membuat pernyataan,",
                     with_meterai: bool = True) -> None:
    pdf.ln(6)
    x = pdf.w - 25 - 70  # right column, 70mm wide
    pdf.set_xy(x, pdf.get_y())
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(70, 6, f"{_PLACE}, {_today_id()}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(x)
    pdf.cell(70, 6, role, align="C", new_x="LMARGIN", new_y="NEXT")
    # meterai + signature box
    box_y = pdf.get_y() + 2
    pdf.set_draw_color(200)
    pdf.rect(x + 12, box_y, 46, 22)
    pdf.set_xy(x + 12, box_y + 8)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(150)
    pdf.cell(46, 5, "Meterai Rp10.000 + Tanda Tangan" if with_meterai else "Tanda Tangan",
             align="C")
    pdf.set_text_color(0)
    pdf.set_draw_color(0)
    pdf.set_xy(x, box_y + 26)
    pdf.set_font("Helvetica", "BU", 11)
    pdf.cell(70, 6, name if name != _MISSING else "( Nama Pemohon )", align="C")
    pdf.ln(10)


# ---------------------------------------------------------------------------
# The three documents
# ---------------------------------------------------------------------------

def _pakta_integritas(data: dict) -> bytes:
    pdf = _new()
    _title(pdf, "PAKTA INTEGRITAS")
    _para(pdf, "Saya yang bertanda tangan di bawah ini:")
    _kv(pdf, "Nama", _v(data, "applicant_name"))
    _kv(pdf, "NIK", _v(data, "nik"))
    _kv(pdf, "Jabatan", _v(data, "jabatan"))
    _kv(pdf, "Nama Perusahaan", _v(data, "business_name"))
    _kv(pdf, "Alamat", _v(data, "alamat"))
    pdf.ln(3)
    _para(pdf,
          f"dalam rangka permohonan {_v(data, 'license_name')} pada Dinas "
          "Penanaman Modal dan Pelayanan Terpadu Satu Pintu (DPMPTSP) Provinsi "
          "Jawa Tengah, dengan ini menyatakan dengan sebenar-benarnya bahwa saya:")
    for i, t in enumerate([
        "Tidak akan melakukan praktik Korupsi, Kolusi, dan Nepotisme (KKN);",
        "Akan menyampaikan dokumen dan data yang benar serta dapat dipertanggungjawabkan;",
        "Akan mematuhi seluruh ketentuan peraturan perundang-undangan yang berlaku;",
        "Bersedia menerima sanksi sesuai ketentuan apabila melanggar pernyataan ini.",
    ], 1):
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(8, 6.5, f"{i}.", new_x="RIGHT", new_y="TOP")
        pdf.multi_cell(0, 6.5, t, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    _para(pdf, "Demikian Pakta Integritas ini saya buat dengan penuh kesadaran "
               "dan tanggung jawab tanpa ada paksaan dari pihak manapun.")
    _signature_block(pdf, _v(data, "applicant_name"))
    return pdf


def _surat_permohonan(data: dict) -> bytes:
    pdf = _new()
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, f"{_PLACE}, {_today_id()}", align="R", new_x="LMARGIN", new_y="NEXT")
    _kv(pdf, "Nomor", "...../...../.....")
    _kv(pdf, "Lampiran", "1 (satu) berkas")
    _kv(pdf, "Perihal", f"Permohonan {_v(data, 'license_name')}")
    pdf.ln(4)
    _para(pdf, "Kepada Yth.\nKepala DPMPTSP Provinsi Jawa Tengah\ndi Semarang")
    _para(pdf, "Dengan hormat,")
    _para(pdf,
          f"Yang bertanda tangan di bawah ini, {_v(data, 'applicant_name')} "
          f"selaku {_v(data, 'jabatan')} dari {_v(data, 'business_name')}, dengan "
          f"ini mengajukan permohonan {_v(data, 'license_name')} dengan data "
          "sebagai berikut:")
    _kv(pdf, "Nama Perusahaan", _v(data, "business_name"))
    _kv(pdf, "Alamat", _v(data, "alamat"))
    _kv(pdf, "Nama Kapal", _v(data, "nama_kapal"))
    _kv(pdf, "Ukuran (GT)", _v(data, "gt_kapal"))
    _kv(pdf, "Jenis Kegiatan", "Pengadaan/Pembangunan Kapal Perikanan")
    pdf.ln(3)
    _para(pdf, "Sebagai kelengkapan permohonan, kami lampirkan dokumen "
               "persyaratan sesuai ketentuan yang berlaku. Atas perhatian dan "
               "persetujuan Bapak/Ibu, kami ucapkan terima kasih.")
    _signature_block(pdf, _v(data, "applicant_name"), role="Hormat kami,")
    return pdf


def _surat_pesanan(data: dict) -> bytes:
    pdf = _new()
    _title(pdf, "SURAT PESANAN", "Pembangunan / Pengadaan Kapal Perikanan")
    _para(pdf, f"Pada hari ini, {_today_id()}, yang bertanda tangan di bawah ini:")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(0, 6.5,
                   f"1. {_v(data, 'applicant_name')}, {_v(data, 'jabatan')} "
                   f"{_v(data, 'business_name')}, selanjutnya disebut PEMESAN.",
                   new_x="LMARGIN", new_y="NEXT")
    pdf.multi_cell(0, 6.5,
                   f"2. {_v(data, 'galangan')}, selanjutnya disebut PELAKSANA "
                   "(galangan kapal).", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    _para(pdf, "PEMESAN memesan pembangunan/pengadaan kapal perikanan kepada "
               "PELAKSANA dengan spesifikasi sebagai berikut:")
    _kv(pdf, "Nama Kapal", _v(data, "nama_kapal"))
    _kv(pdf, "Ukuran (GT)", _v(data, "gt_kapal"))
    _kv(pdf, "Bahan/Material", _v(data, "bahan_kapal"))
    _kv(pdf, "Peruntukan", "Kapal Penangkap/Pengangkut Ikan")
    pdf.ln(3)
    _para(pdf, "Demikian Surat Pesanan ini dibuat dengan sebenarnya untuk "
               "dipergunakan sebagai kelengkapan permohonan perizinan.")
    _signature_block(pdf, _v(data, "applicant_name"), role="PEMESAN,")
    return pdf


_GENERATORS = {
    PAKTA_INTEGRITAS: _pakta_integritas,
    SURAT_PERMOHONAN: _surat_permohonan,
    SURAT_PESANAN: _surat_pesanan,
}


def generate(doc_type: str, data: dict, *, encrypt_password: str | None = None) -> bytes:
    """Render `doc_type` as a filled PDF (bytes). Raises KeyError on unknown type.
    Missing data fields render as a blank line — never invented.

    If `encrypt_password` is given, the PDF is AES-128 encrypted: it opens ONLY
    with that password. The flow passes the citizen's NIK — which they already
    know and which is never transmitted — so a leaked download URL yields an
    unreadable file. The owner password is random (nobody holds owner rights),
    and default permissions (3900 = all) are kept so the citizen can still
    print / sign / e-meterai it via Mekari.
    """
    gen = _GENERATORS.get(doc_type)
    if gen is None:
        raise KeyError(f"unknown doc_type: {doc_type!r}")
    pdf = gen(data or {})
    pwd = str(encrypt_password).strip() if encrypt_password else ""
    if pwd:
        owner = secrets.token_urlsafe(18)  # random → nobody holds owner rights
        try:
            pdf.set_encryption(
                owner_password=owner, user_password=pwd,
                encryption_method=EncryptionMethod.AES_128,
            )
        except Exception:  # AES needs `cryptography`; fall back to RC4 if absent
            logger.warning("AES-128 unavailable — using RC4 for encrypted %s", doc_type)
            pdf.set_encryption(
                owner_password=owner, user_password=pwd,
                encryption_method=EncryptionMethod.RC4,
            )
    out = bytes(pdf.output())
    logger.info("Generated %s | bytes=%d | encrypted=%s", doc_type, len(out), bool(pwd))
    return out
