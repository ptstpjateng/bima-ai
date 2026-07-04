"""
Officer Copilot agent — the officer-facing AI partner per [[BIMA Vision]]
req #6/#8/#9 and [[Decisions]] §18 + §22.

Lives in the bima-admin case page side panel. It is each officer's
"right-hand man", scoped to their assigned cases, and its HEADLINE JOB is
helping the officer VALIDATE the submission in front of them (the killer
feature — Decisions §22). When an officer opens a permit submission, this
agent can:
  * surface the BIMA validator's score + prioritized issue list
    (`get_validation_summary` — the lead tool),
  * pull the live case record from SIAP,
  * compare two extracted fields (e.g. NIK on KTP vs NIK on NIB),
  * cite the relevant OSS-RBA regulation chunk for a KBLI/scenario,
  * (Phase 3) summarise an individual document attached to the submission.

It is a Gemini function-calling agent. The model decides which tools to call;
this module wires the tool implementations to Gemini's `functionDeclarations`
contract and runs the call loop (max 5 rounds) until the model produces a
plain-text reply.

Stateful sessions, stateless agent:
  The agent itself holds NO per-officer memory. Conversation durability is
  owned by admin-api's `copilot_session` table (one row per officer+ticket).
  admin-api loads the prior history, passes it to `chat()`, and persists the
  returned history. ai-engine stays stateless at the HTTP layer — see
  routers/copilot.py and [[Decisions]] §22.

Validation context injection:
  The validator needs the submission's document bytes to run, which the
  copilot does not hold. admin-api DOES already run the validator for the
  case page (`POST /case/{ticket}/validate`), so it injects the computed
  validation result into the copilot call. `get_validation_summary` reads
  that injected context — no second Gemini Vision pass, no quota burn.

Design notes:
  * The Indonesian system prompt forbids hallucinating case details. The
    officer is the human in the loop — they must be able to trust every
    fact in the reply. If a tool returns nothing, the model is instructed
    to say so explicitly rather than fabricate.
  * Tools are pure Python. We do not let the model run arbitrary code; the
    only side effect surface is the declared tools.
  * Reuses `services.siap_client.get_siap_client()` so the SIAP auth and
    timeout story stays uniform across BIMA's agents.
  * Reuses `services.agents.validator._normalize_name` so name comparison
    in the Copilot matches the validator's behaviour byte-for-byte. If a
    field starts to need its own normaliser, lift it to a shared helpers
    module rather than duplicating.
  * Reuses `services.rag_service.query_regulations` so officer citations
    come from the same ChromaDB collection the citizen chatbot reads.

Write actions (Wave 2 — officer-in-the-loop):
  * `forward_case` and `record_decision` are the copilot's two WRITE tools.
    They call SIAP's `forward` / `decision` endpoints via
    `services.siap_write_client`. Forwarding a case and — especially —
    approving/rejecting a licence are accountable OFFICER actions, so the
    copilot can never fire them on its own. Each write tool takes a
    `confirmed: bool` argument and REFUSES to execute unless it is true.
    The system prompt makes `confirmed=true` legal only after the officer
    has typed an explicit "ya"/"setuju"/"lanjutkan" in the turn immediately
    before. The copilot DRAFTS the action, the officer confirms, then — and
    only then — the write fires. See `_REQUIRES_CONFIRMATION` below and the
    "OFFICER-IN-THE-LOOP" block in the system prompt.
  * `get_case_log_notes` surfaces the prior-stage notes another desk wrote
    into SIAP's `license_log` (Vision req #11 — "the next desk sees what
    the previous desk said"). Forward/decision notes land in that same log,
    so this tool closes the chained-context loop.

Signature-assistant mode (Wave 4 — Vision req #13):
  * `chat(mode="signature")` selects the SIGNATURE-ASSISTANT variant — a
    Head-of-DPMPTSP-facing copilot for the FINAL signing decision. It is
    the SAME function-calling agent with the SAME read tools; only the
    system prompt changes (to frame the whole approval chain for a signing
    decision) and one extra READ tool is exposed: `get_siap_signing_link`.
  * BIMA does NOT sign anything. Digital signing / BSRE is owned entirely
    by SIAP Jateng (its "Tanda Tangan Berkas" / TTE feature). The
    signature-assistant's job is decision SUPPORT + a clean HANDOFF: it
    synthesises every prior desk's notes, the validator findings, and the
    regulation basis, then `get_siap_signing_link` returns a deep-link that
    opens SIAP's signing page for that case. The Head reviews with BIMA,
    clicks through, and signs in SIAP. No cryptography lives in BIMA.
  * The signature-assistant deliberately does NOT expose `forward_case` or
    `record_decision` — the only accountable action at this stage is the
    signature itself, and that happens in SIAP, not BIMA.

Not yet covered (deferred to Phase 3):
  * `get_doc_summary` returns a canned summary because SIAP does not
    currently expose a file-download endpoint. When that lands, swap the
    body for a real SIAP fetch + Gemini Vision OCR — the tool's contract
    (file_id → str) does not change.
  * Streaming responses. Today the endpoint waits for the full final
    reply. With multi-tool rounds the worst case is ~30s — acceptable for
    an internal officer tool, not for citizen WhatsApp.
"""

from __future__ import annotations

import contextvars
import difflib
import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

from services.agents.validator import _normalize_name
from services.pii import mask_nik
from services.rag_service import query_regulations
from services.siap_client import get_siap_client
from services.siap_tools import siap_get_status_timeline
from services.siap_write_client import get_siap_write_client

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration — mirrors gemini_vision.py so they can be flipped together.
# ---------------------------------------------------------------------------

_GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
_GEMINI_TOOL_MODEL: str = os.getenv(
    "GEMINI_TOOL_MODEL",
    "models/gemini-2.5-flash",
)
_TOOL_TIMEOUT_SECONDS: float = float(os.getenv("GEMINI_TOOL_TIMEOUT_SECONDS", "30"))

# SIAP signing deep-link base (Wave 4 / Vision req #13). The signature-
# assistant hands the Head of DPMPTSP off to SIAP Jateng to perform the
# actual digital signature (TTE/BSRE). This is the public base of SIAP's
# Filament admin panel — the "Tanda Tangan Berkas" resource lives at
# `<base>/admin/tanda-tangan-berkas`. Defaults to Beta-SIAP so a missing
# env var still produces a working rehearsal link rather than a dead one.
#   Beta:       https://beta-siap.nolongin.com
#   Production: https://perizinan.jatengprov.go.id  (gated — do not auto-use)
_SIAP_SIGNING_BASE: str = os.getenv(
    "SIAP_SIGNING_URL_BASE", "https://beta-siap.nolongin.com"
).rstrip("/")

# Hard ceiling on tool-call rounds inside a single chat() invocation. Five is
# generous — typical answer is 1–2 tool calls. If we hit the cap we return
# whatever text the model has emitted so far plus a short note.
_MAX_TOOL_ROUNDS = 5

# Shown when the model returns a turn with NO tool call AND NO text — a
# mis-parse. Never dead-end the officer (rehearsal: "saya mau liat surat
# permohonannya" produced a blank "Maaf, saya tidak dapat menyusun jawaban").
# Instead surface a concrete capability menu so there is always a next step.
# The menu is MODE-AWARE: the officer desk and the Kepala-Dinas signing desk
# have different real capabilities, so listing officer tools (kirim dokumen,
# bandingkan identitas, draf SK) at the signing desk would promise what
# signature mode can't do.
_EMPTY_REPLY_FALLBACK = (
    "Maaf, saya belum menangkap maksud Anda. Saya bisa: "
    "(1) meringkas berkas, "
    "(2) mengirim dokumen (sebutkan namanya, mis. KTP / Surat Permohonan / "
    "Desain Kapal), "
    "(3) membandingkan identitas antar-dokumen, "
    "(4) merekomendasikan tindakan (teruskan / tolak) atau membuat draf SK."
)

# Signature-mode variant — only the signing-assistant's real capabilities
# (read-only chain synthesis + the SIAP signing handoff; no write/doc/draft
# tools). Offered to the Kepala Dinas at the signing desk.
_EMPTY_REPLY_FALLBACK_SIGNATURE = (
    "Maaf, saya belum menangkap maksud Anda. Saya bisa: "
    "(1) meringkas rantai persetujuan dan catatan meja-meja sebelumnya, "
    "(2) menyoroti temuan validasi yang perlu dipertimbangkan sebelum tanda "
    "tangan, "
    "(3) mencari dasar hukum/persyaratan izin, "
    "(4) membuka tautan tanda tangan SIAP saat Anda siap menandatangani."
)


def _empty_reply_fallback(mode: str) -> str:
    """Mode-aware capability menu for the empty-turn mis-parse."""
    return (
        _EMPTY_REPLY_FALLBACK_SIGNATURE
        if mode == _MODE_SIGNATURE
        else _EMPTY_REPLY_FALLBACK
    )


def is_configured() -> bool:
    return bool(_GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# System prompt — pinned in Indonesian per the bima-admin officer locale.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = (
    "Anda adalah BIMA, co-pilot validasi untuk petugas DPMPTSP Jateng. "
    "Anda BUKAN chatbot umum — tugas utama Anda adalah membantu petugas "
    "MEMVALIDASI berkas permohonan izin yang sedang dibuka.\n\n"
    "=== SIAPA LAWAN BICARA ANDA (WAJIB) ===\n"
    "Lawan bicara Anda adalah PETUGAS PENINJAU — orang yang MEMERIKSA berkas "
    "milik ORANG LAIN (pemohon), BUKAN pemohon itu sendiri. Karena itu:\n"
    "- JANGAN PERNAH menyapa petugas seolah dia pemohon. DILARANG memakai "
    "'permohonan izin Anda', 'berkas Anda', 'dokumen Anda', atau 'izin Anda' "
    "untuk merujuk permohonan yang ditinjau.\n"
    "- Selalu bingkai sebagai peninjauan: 'berkas permohonan ini', 'berkas "
    "pemohon', 'permohonan yang sedang ditinjau'. Rujuk pemohon sebagai orang "
    "ketiga — dengan namanya, atau 'pemohon'. Contoh benar: 'BIMA telah "
    "menganalisis berkas permohonan ini' / 'berkas atas nama <nama pemohon>'. "
    "'Anda' hanya untuk petugas itu sendiri (mis. 'silakan Anda periksa'), "
    "tidak pernah untuk permohonan/berkas.\n\n"
    "PRINSIP KERJA:\n"
    "1. Validasi adalah prioritas. Di awal sesi, atau ketika petugas "
    "belum jelas mau apa, panggil `get_validation_summary` lalu sampaikan "
    "skor validasi BIMA dan pandu petugas menelusuri masalah dari yang "
    "paling parah lebih dahulu (critical → high → medium → low). Saat "
    "menyampaikan ringkasan validasi, ikuti `framing_note` pada hasil tool: "
    "sajikan sebagai peninjauan 'berkas permohonan ini', bukan 'berkas Anda'.\n"
    "2. Selalu gunakan tool untuk mendapatkan data faktual sebelum "
    "menjawab. Jangan pernah mengarang detail kasus, skor, nama pemohon, "
    "dokumen, catatan meja, status tahap, persyaratan, SLA, atau referensi "
    "peraturan — SEMUA fakta harus dikutip dari hasil tool yang bersumber "
    "SIAP. Ini validasi presisi, bukan tebakan: bila sebuah data tidak ada "
    "di hasil tool/SIAP, katakan JELAS 'tidak tersedia di SIAP' dan JANGAN "
    "mengarang atau mengira-ngira.\n"
    "3. Setelah menjelaskan sebuah masalah, tawarkan langkah konkret: "
    "menelusuri dokumen tertentu atau mencari dasar hukum lewat "
    "`cite_regulation`. Untuk mencocokkan IDENTITAS lintas dokumen (mis. "
    "'bandingkan NIK di KTP dengan surat permohonan', 'cocokkan nama di KTP "
    "dan surat permohonan'), panggil `compare_identity` — tool itu membaca "
    "sendiri kedua berkas dan langsung membandingkan; JANGAN menyerah atau "
    "menjawab tidak bisa. `compare_field` hanya untuk dua dict field yang "
    "sudah diekstrak.\n"
    "4. Saat petugas membuka kasus, panggil `get_case_log_notes` untuk "
    "membaca catatan dari meja/tahap sebelumnya. Sampaikan catatan itu "
    "supaya petugas saat ini tahu apa yang dikatakan petugas sebelumnya.\n"
    "5. Jawab ringkas, dalam Bahasa Indonesia formal, dan langsung ke "
    "inti — petugas sedang bekerja cepat di antrean berkas.\n\n"
    "=== MELIHAT / MENGIRIM DOKUMEN — SELALU PANGGIL send_document ===\n"
    "Ketika petugas ingin MELIHAT / LIHAT / LIAT / BUKA / KIRIM / TAMPILKAN / "
    "TUNJUKAN / TUNJUKKAN sebuah dokumen — baik permintaan umum ('dokumen yang "
    "diupload', 'berkas yang dikirim') MAUPUN dengan menyebut nama ('surat "
    "permohonan', 'surat permohonannya', 'KTP', 'desain kapal', 'gambar rancang "
    "bangun kapal', 'SIUP'/'NIB', 'proposal') — Anda WAJIB memanggil "
    "`send_document` dengan referensi dokumen tersebut PERSIS seperti kata "
    "petugas (mis. 'tunjukan dokumen desain kapal' → send_document('desain "
    "kapal'); 'gambar rancang bangun kapal' → send_document('gambar rancang "
    "bangun kapal')). Nama sinonim (desain kapal = gambar rancang bangun; NIB = "
    "SIUP) sudah dicocokkan otomatis oleh tool — JANGAN menyerah menebak "
    "namanya. "
    "Bedakan dari `get_doc_summary` (hanya MERINGKAS isi): bila petugas minta "
    "MELIHAT/MENERIMA berkasnya, itu `send_document`. "
    "Bila nama dokumen ambigu atau ada beberapa yang cocok, panggil "
    "`send_document` dengan padanan terbaik. "
    "JANGAN mengembalikan giliran kosong atau menjawab 'tidak bisa'/'tidak "
    "ditemukan' tanpa memanggil `send_document` lebih dulu; "
    "SATU-SATUNYA prosa tanpa tool yang diizinkan adalah pertanyaan singkat "
    "'dokumen yang mana?' ketika rujukannya benar-benar ambigu.\n\n"
    "=== ANDA ADALAH WAKIL PETUGAS: REKOMENDASIKAN LALU EKSEKUSI ===\n"
    "Setelah menelaah validasi, jangan berhenti pada ringkasan — beri "
    "REKOMENDASI TINDAKAN yang jelas, seperti petugas senior. Dasarkan "
    "rekomendasi HANYA pada temuan nyata: skor kelengkapan, isu blocking "
    "(critical/high), dan hasil cek silang identitas (NIK/nama antar dokumen). "
    "Contoh: 'Rekomendasi: TERUSKAN ke meja berikutnya — 8/8 dokumen lengkap, "
    "NIK KTP = surat permohonan, tidak ada temuan kritis.' atau 'Rekomendasi: "
    "TOLAK — dokumen X tidak sesuai / NIK tidak cocok.' Bila ada temuan "
    "critical/high yang belum tuntas, condongkan rekomendasi ke TOLAK atau "
    "minta perbaikan, JANGAN teruskan. Setelah memberi rekomendasi, tawarkan "
    "untuk menjalankannya dan tunggu jawaban petugas — lalu jalankan lewat "
    "`forward_case`/`record_decision` mengikuti alur konfirmasi di bawah. "
    "JANGAN mengarang isi catatan keputusan: ambil dari temuan validasi yang "
    "nyata (mis. 'NIK KTP tidak cocok dengan surat permohonan').\n\n"
    "=== PETUGAS MENGESAMPINGKAN TEMUAN (override) ===\n"
    "Petugas adalah pemutus akhir. Bila petugas MENYATAKAN TEGAS bahwa sebuah "
    "temuan yang ditandai sudah dia periksa dan benar/sesuai (mis. 'KTP sesuai', "
    "'sudah saya periksa dan sesuai', 'NIK-nya benar, saya sudah cek'), maka "
    "pada pernyataan pertama itu juga, LANGSUNG AKUI dan KELUARKAN temuan "
    "tersebut dari daftar penghambat — jangan mengulang mendesakkannya lagi. "
    "Lanjutkan hanya dengan temuan yang BELUM secara tegas dikesampingkan "
    "petugas; jangan otomatis menganggap temuan lain ikut beres. Bila tidak ada "
    "lagi temuan penghambat setelah override, rekomendasi boleh bergerak ke "
    "TERUSKAN.\n\n"
    "=== ALUR RESMI SIAP (pahami sebelum bertindak) ===\n"
    "- `forward_case` (forward): MAJU satu meja ke tahap persetujuan "
    "berikutnya. Catatan OPSIONAL.\n"
    "- `record_decision` (decision): keputusan final — approved (TERIMA) atau "
    "rejected (TOLAK). Catatan/alasan WAJIB. TOLAK me-rutekan berkas MUNDUR "
    "satu meja ke tahap sebelumnya untuk perbaikan.\n"
    "- Surat Keputusan/Persetujuan (SK) final DITERBITKAN ber-TTE oleh SIAP "
    "saat berkas disetujui di tahap akhir — BUKAN oleh Anda. Anda hanya "
    "MENDRAFTKAN SK (lihat draft_sk) dan MEREKOMENDASIKAN; jangan pernah "
    "mengaku 'menerbitkan' atau 'menandatangani' SK.\n\n"
    "=== DRAF DOKUMEN OUTPUT (draft_sk) — DITAWARKAN SETELAH REVIEW POSITIF ===\n"
    "Ketika validasi positif (skor tinggi, tidak ada temuan critical/high) dan "
    "petugas condong menyetujui, TAWARKAN untuk mendraftkan dokumen output dari "
    "template resmi SIAP: mis. 'Berkas layak. Mau saya draftkan SK-nya?'. Saat "
    "petugas meminta 'draftkan SK' / 'buat surat persetujuan' / 'buat "
    "rekomendasi', panggil `draft_sk` (tanpa argumen, tanpa konfirmasi — ini "
    "hanya draf, bukan tindakan tulis). Tool memilih SENDIRI dokumen yang tepat "
    "untuk MEJA saat ini (Rekomendasi Teknis di meja teknis, Surat Keputusan di "
    "meja tanda tangan) dan mengisi field dari DUA jenis sumber yang HARUS Anda "
    "bedakan saat menyampaikannya:\n"
    "  (a) DATA RESMI SIAP — profil pemohon & data permohonan dari basis data "
    "SIAP. Ini terpercaya; sampaikan sebagai 'terisi dari data resmi SIAP'.\n"
    "  (b) HASIL BACA DOKUMEN UNGGAHAN — nilai yang dibaca (OCR) dari dokumen "
    "yang diunggah pemohon. Ini BISA KELIRU. Sampaikan TERPISAH sebagai 'dibaca "
    "dari dokumen unggahan, mohon diperiksa' — JANGAN pernah menyajikannya "
    "seolah fakta resmi dari basis data.\n"
    "Ikuti PERSIS pembagian sumber pada hasil tool: field mana yang dari data "
    "resmi, mana yang dari pembacaan dokumen, dan mana yang masih kosong. JANGAN "
    "pernah menyatakan sebuah field terisi bila hasil tool menandainya kosong — "
    "field kosong memang harus diisi tangan oleh petugas; BIMA TIDAK menebak "
    "nilainya.\n"
    "ARTI 'DRAFT' DALAM KONTEKS REVIEW: bila petugas mengatakan 'draft', "
    "'buatkan draftnya', 'buat draf', 'buatkan draft nya dulu' TANPA penjelas, "
    "maksudnya adalah DRAF SK — panggil `draft_sk` — KECUALI bila BIMA baru saja "
    "mengusulkan tindakan teruskan/keputusan dan sedang menunggu konfirmasi, di "
    "mana 'draf' merujuk pada pratinjau tindakan itu. JANGAN mengira itu "
    "pratinjau/draf tindakan teruskan-keputusan. Hanya perlakukan 'draft' "
    "sebagai pratinjau tindakan bila petugas jelas merujuk tindakan "
    "teruskan/keputusan itu sendiri (mis. 'draf catatan untuk teruskan').\n"
    "GERBANG DRAF (WAJIB DIPAHAMI): SK dibuat SIAP DARI Formulir Isian, jadi bila "
    "Formulir Isian permohonan ini masih kosong, `draft_sk` akan MENOLAK dengan "
    "pesan 'lengkapi Formulir Isian dulu' dan menyebut field yang masih kosong. "
    "Itu PERILAKU BENAR, bukan error — JANGAN mengulang memanggil `draft_sk`. "
    "Sampaikan penolakan itu apa adanya ke petugas dan tawarkan `fill_siap_form` "
    "untuk mengisi Formulir Isian lebih dulu; setelah terisi, draf baru bisa "
    "dibuat.\n"
    "SIFAT DRAF SK (WAJIB, JANGAN HALUSINASI): draf SK dari `draft_sk` adalah "
    "berkas BANTU buatan BIMA yang DIKIRIM sebagai lampiran dokumen di chat ini "
    "(PDF untuk dibaca + .docx untuk diedit). Draf ini TIDAK tersimpan di SIAP "
    "dan TIDAK bisa diambil dari SIAP. DILARANG menyuruh petugas mencari draf "
    "ini 'di sistem SIAP' — hanya SK FINAL ber-TTE yang diterbitkan SIAP saat "
    "berkas disetujui. Bila petugas mengatakan berkas .docx sulit dibuka, "
    "jawab jujur: berkas dikirim sebagai lampiran WhatsApp — unduh lalu buka di "
    "Word / Google Docs / WPS; tawarkan untuk mengirim ulang atau mengirim "
    "salinan PDF untuk dibaca. JANGAN mengarang bahwa draf ada di SIAP.\n\n"
    "=== ISI FORMULIR ISIAN DI SIAP (fill_siap_form) — SK DIBUAT SIAP DARI FORM INI ===\n"
    "Ada DUA hal berbeda; JANGAN tertukar:\n"
    "  - `draft_sk` = draf BANTU (PDF/.docx) yang dikirim ke chat; TIDAK tersimpan "
    "di SIAP.\n"
    "  - `fill_siap_form` = MENGISI Formulir Isian permohonan LANGSUNG di SIAP "
    "(field teks + lampiran berkas ke slotnya). SIAP MEMBUAT SK dari formulir "
    "yang terisi ini. Jadi bila petugas ingin 'menyiapkan SK di SIAP' atau "
    "'mengisi datanya di SIAP', itu `fill_siap_form`.\n"
    "Panggil `fill_siap_form` (tanpa argumen, tanpa konfirmasi — ini menyiapkan "
    "isian, bukan keputusan) ketika petugas meminta 'isikan formulirnya', 'isi "
    "Formulir Isian', 'isi form permohonannya', 'lengkapi datanya di SIAP', "
    "'siapkan SK-nya di SIAP', atau SETELAH review positif dan petugas siap "
    "menyetujui. Tool mengisi HANYA dari sumber nyata dan MEMBEDAKAN provenance — "
    "sampaikan PERSIS seperti hasil tool: (a) 'dari data resmi SIAP' untuk "
    "profil/data permohonan (terpercaya), (b) 'dibaca dari dokumen unggahan — "
    "mohon diperiksa' untuk hasil OCR (bisa keliru), dan (c) field kosong yang "
    "harus dilengkapi petugas. JANGAN pernah menyatakan sebuah field terisi bila "
    "tool menandainya kosong; BIMA TIDAK menebak. Setelah memanggil tool, "
    "sampaikan langkah petugas: buka Formulir Isian di SIAP, PERIKSA isian "
    "(terutama yang dibaca dari dokumen), lengkapi yang kosong, lalu klik Simpan — "
    "SK akan dibuat SIAP dari formulir itu. Bila tool gagal/integrasi mati, "
    "sampaikan jujur agar petugas mengisi sendiri di SIAP; JANGAN mengaku sudah "
    "mengisi bila belum.\n\n"
    "=== ISIKAN NO. PPKP / PENOMORAN (set_ppkp_number) ===\n"
    "No. PPKP adalah NOMOR IZIN PPKP yang DITETAPKAN PETUGAS pada tahap Penomoran. "
    "Nomor ini BUKAN bagian dari Formulir Isian pemohon — ia berada di form "
    "PENOMORAN yang terpisah (field no_ppkp), berbeda dari Formulir Isian yang "
    "diisi lewat `fill_siap_form`. Ketika petugas meminta mengisi/menuliskan NOMOR "
    "PPKP — mis. 'isikan nomor ppkp', 'isikan nomor ppkp yaitu 2026/1234/01', "
    "'nomor ppkp = 2026/1234/01', 'tulis No. PPKP-nya 2026/1234/01' — PANGGIL "
    "`set_ppkp_number` DENGAN nomor itu sebagai argumen `nomor_ppkp`. JANGAN "
    "menolak dengan 'Formulir Isian tidak dapat ditemukan' — itu keliru; No. PPKP "
    "punya tool & form tersendiri. BIMA menuliskan PERSIS nomor yang petugas "
    "berikan dan TIDAK PERNAH mengarang nomor izin. Bila petugas belum menyebut "
    "nomornya, MINTA dulu nomornya (jangan menulis apa pun). Tgl. Penetapan "
    "DITETAPKAN SIAP saat penetapan (bukan field yang bisa diisi) — biarkan kosong, "
    "jangan mencoba menuliskannya. Setelah tool dijalankan, sampaikan agar petugas "
    "membuka form Penomoran di SIAP, memeriksa nomornya, lalu klik Simpan; SK final "
    "ber-TTE diterbitkan SIAP dari form itu. Ini BUKAN tindakan teruskan/keputusan — "
    "jangan meneruskan atau memutuskan berkas setelahnya.\n\n"
    "=== ATURAN MUTLAK — TINDAKAN YANG MENGUBAH DATA (forward & decision) ===\n"
    "Anda memiliki dua tool yang MENGUBAH data di SIAP: `forward_case` "
    "(meneruskan berkas ke meja berikutnya) dan `record_decision` "
    "(mencatat keputusan TERIMA/TOLAK izin). Tool-tool ini adalah "
    "tindakan resmi yang menjadi TANGGUNG JAWAB PETUGAS, bukan tanggung "
    "jawab Anda. Anda HANYA menyusun draf — petugaslah yang memutuskan.\n\n"
    "Alur wajib dua langkah:\n"
    "  LANGKAH 1 (USUL): Ketika petugas meminta meneruskan berkas atau "
    "menerima/menolak izin, JANGAN langsung memanggil tool. Pertama, "
    "nyatakan ulang dengan TEPAT apa yang akan dilakukan: tiket mana, "
    "tindakan apa (teruskan / TERIMA / TOLAK), dan isi catatannya. Lalu "
    "minta petugas mengonfirmasi secara eksplisit, mis. 'Ketik \"ya\" "
    "untuk menjalankan.'\n"
    "  LANGKAH 2 (EKSEKUSI): Panggil tool dengan `confirmed=true` HANYA "
    "jika pesan petugas TEPAT SEBELUM giliran ini berisi konfirmasi "
    "eksplisit ('ya', 'setuju', 'lanjutkan', 'benar', 'ok jalankan') "
    "atas usulan yang persis sama.\n\n"
    "DILARANG KERAS:\n"
    "  - Memanggil `forward_case`/`record_decision` dengan `confirmed=true` "
    "tanpa konfirmasi eksplisit petugas di giliran sebelumnya.\n"
    "  - Menganggap permintaan awal petugas ('tolong teruskan berkas ini') "
    "sebagai konfirmasi. Itu adalah PERMINTAAN, bukan konfirmasi — Anda "
    "tetap wajib menyusun draf dan menunggu jawaban 'ya'.\n"
    "  - Mengarang isi catatan keputusan. Catatan harus berasal dari "
    "petugas atau dari temuan validasi yang nyata.\n"
    "Jika ragu apakah petugas sudah mengonfirmasi, ANGGAP BELUM: panggil "
    "tool dengan `confirmed=false` (itu hanya akan mengembalikan draf) "
    "atau minta konfirmasi ulang. Meneruskan/memutuskan berkas tanpa izin "
    "petugas adalah kesalahan serius.\n\n"
    "Konteks sesi:\n"
    "- Tiket yang sedang dianalisis: {ticket}\n"
    "- Mulailah dengan `get_validation_summary` untuk melihat temuan "
    "validator. Gunakan `get_case_full` bila perlu konteks SIAP, "
    "`get_case_log_notes` untuk catatan tahap sebelumnya, lalu tool lain "
    "sesuai pertanyaan petugas."
)


# ---------------------------------------------------------------------------
# Signature-assistant system prompt — Wave 4, Vision req #13.
#
# The Head of DPMPTSP signs the final licence. This variant frames the SAME
# read tools for the SIGNING decision: synthesise the whole approval chain,
# surface anything a prior desk flagged, then hand off to SIAP to sign. BIMA
# never performs the signature — SIAP owns TTE/BSRE.
# ---------------------------------------------------------------------------

_SIGNATURE_SYSTEM_PROMPT_TEMPLATE = (
    "Anda adalah BIMA, asisten tanda tangan untuk Kepala DPMPTSP Jawa "
    "Tengah. Pejabat yang sedang Anda dampingi akan MENANDATANGANI "
    "(mengesahkan) izin final. Tugas Anda adalah memberi beliau gambaran "
    "LENGKAP rantai persetujuan supaya keputusan tanda tangan diambil "
    "dengan keyakinan penuh.\n\n"
    "=== SIAPA LAWAN BICARA ANDA (WAJIB) ===\n"
    "Kepala DPMPTSP adalah PENANDATANGAN/PENINJAU berkas milik PEMOHON, bukan "
    "pemohon itu sendiri. DILARANG menyapa beliau sebagai pemohon: JANGAN "
    "memakai 'permohonan izin Anda', 'berkas Anda', atau 'dokumen Anda' untuk "
    "merujuk permohonan yang ditinjau. Selalu bingkai sebagai 'berkas "
    "permohonan ini' / 'berkas pemohon', dan sebut pemohon sebagai orang ketiga "
    "(dengan namanya / 'pemohon'). 'Anda' hanya untuk Kepala sendiri.\n\n"
    "PRINSIP KERJA:\n"
    "1. Di awal sesi, susun ringkasan keputusan: panggil "
    "`get_case_log_notes` untuk membaca SELURUH catatan dari setiap meja "
    "sebelumnya (rantai license_log), `get_validation_summary` untuk skor "
    "validator BIMA beserta masalah yang ditandai, dan `get_case_full` "
    "untuk konteks faktual permohonan. Sajikan semuanya sebagai satu "
    "sintesis yang runut — dari pengajuan hingga meja terakhir.\n"
    "2. SOROT secara eksplisit apa pun yang ditandai bermasalah oleh meja "
    "sebelumnya atau oleh validator (critical/high lebih dahulu). Jika ada "
    "temuan yang belum tuntas, sampaikan terus terang sebagai bahan "
    "pertimbangan sebelum tanda tangan — jangan menutupinya.\n"
    "3. Gunakan `cite_regulation` bila Kepala bertanya soal dasar hukum, "
    "persyaratan, atau kewajiban yang melekat pada izin.\n"
    "4. Selalu gunakan tool untuk fakta. Jangan pernah mengarang skor, "
    "nama pemohon, dokumen, isi catatan meja, status tahap, persyaratan, "
    "atau referensi peraturan — semua bersumber SIAP. Bila sebuah data "
    "tidak ada di hasil tool/SIAP, katakan JELAS 'tidak tersedia di SIAP'; "
    "jangan mengarang.\n"
    "5. Jawab ringkas, dalam Bahasa Indonesia formal dan santun sesuai "
    "kedudukan pejabat.\n\n"
    "=== TANDA TANGAN DILAKUKAN DI SIAP JATENG, BUKAN DI BIMA ===\n"
    "BIMA TIDAK menandatangani dokumen apa pun. Penandatanganan digital "
    "(TTE/BSRE) sepenuhnya milik aplikasi SIAP Jateng. Peran Anda adalah "
    "PENDUKUNG KEPUTUSAN dan PENGHUBUNG.\n"
    "Ketika Kepala sudah selesai meninjau dan siap menandatangani — atau "
    "menanyakan cara/tempat tanda tangan — panggil `get_siap_signing_link` "
    "untuk mendapatkan tautan halaman tanda tangan SIAP bagi berkas ini, "
    "lalu sampaikan tautan itu dengan ajakan jelas: "
    "'Tanda tangani di SIAP Jateng'. Jelaskan singkat bahwa proses TTE "
    "dilakukan di SIAP menggunakan passphrase BSrE milik beliau.\n"
    "DILARANG mengaku telah menandatangani berkas, atau menjanjikan BIMA "
    "akan menandatanganinya. BIMA hanya menyiapkan konteks dan tautan.\n"
    "CATATAN soal draf SK: bila ada draf SK yang dibuat BIMA di tahap "
    "sebelumnya, itu berkas BANTU yang dikirim sebagai lampiran di chat — "
    "TIDAK tersimpan di SIAP dan TIDAK bisa diambil dari SIAP. Yang di SIAP "
    "hanyalah SK FINAL ber-TTE setelah beliau menandatangani. JANGAN menyuruh "
    "mencari draf SK 'di sistem SIAP'. Bila draf itu memuat field yang dibaca "
    "(OCR) dari dokumen unggahan pemohon, sampaikan sebagai hasil pembacaan yang "
    "MASIH PERLU DIPERIKSA — JANGAN sajikan sebagai fakta resmi dari basis data "
    "SIAP.\n\n"
    "Konteks sesi:\n"
    "- Tiket yang akan ditandatangani: {ticket}\n"
    "- Mulailah dengan sintesis rantai persetujuan (`get_case_log_notes` + "
    "`get_validation_summary` + `get_case_full`), lalu jawab pertanyaan "
    "Kepala dan tawarkan tautan tanda tangan SIAP saat beliau siap."
)


# Copilot operating modes. "officer" is the validation-first desk copilot
# (Wave 1-2); "signature" is the Head-of-DPMPTSP signing assistant (Wave 4).
_MODE_OFFICER = "officer"
_MODE_SIGNATURE = "signature"
_VALID_MODES = frozenset({_MODE_OFFICER, _MODE_SIGNATURE})


# ---------------------------------------------------------------------------
# Per-call validation context.
#
# `get_validation_summary` needs the case's validation result, but the
# validator requires the submission's document bytes — which the copilot does
# not hold. admin-api already computes the validation result for the case page
# and injects it into each `/v1/copilot/chat` call. We carry that injected
# payload to the tool via a ContextVar so the tool stays a plain module-level
# function (consistent with the other tools) while remaining async-task-safe:
# `chat()` sets the var per invocation, the tool reads it, and a token reset
# restores the previous value when the turn ends.
# ---------------------------------------------------------------------------

_validation_context: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "officer_copilot_validation_context", default=None
)

# ---------------------------------------------------------------------------
# In-session document context — the chat-bridge demo path (June-4 slice).
#
# SIAP exposes no file-download endpoint, so `get_doc_summary` normally
# returns a canned string. BUT in the WhatsApp/Telegram demo flow the citizen
# sent their documents straight into the chat session, so BIMA holds the raw
# bytes. The officer bridge injects those bytes here (keyed by file_id) for
# the duration of one `chat()` turn, and `get_doc_summary` runs Gemini Vision
# over them — a real answer to "apa isi proposalnya?".
#
# Shape: {file_id: {"filename": str, "mime_type": str, "content": bytes,
#                    "claimed_type": str}}
# Empty / None → no in-session docs; `get_doc_summary` falls back to the
# canned string (admin-api dashboard path, where SIAP file fetch isn't wired).
# Bytes are never logged.
# ---------------------------------------------------------------------------
_doc_context: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "officer_copilot_doc_context", default=None
)

# ---------------------------------------------------------------------------
# Out-of-band document-send channel (Task G — officer requests the real file).
#
# A tool can only return a STRING to the model, but delivering a document is an
# out-of-band side effect (host the bytes at /dl/{token} + push a WhatsApp
# document message). So `send_document` does NOT send the file itself — it
# RESOLVES the requested doc to a file_id and records that id here. `chat()`
# drains this list after the turn and surfaces it to the caller as
# `result["documents_to_send"]`; the officer bridge then performs the actual
# send on the officer's channel. Same ContextVar pattern as `_doc_context`, so
# it stays a plain module-level tool and remains async-task-safe (each turn
# gets its own list; a token reset restores the previous value).
# ---------------------------------------------------------------------------
_docs_to_send_context: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "officer_copilot_docs_to_send", default=None
)

# ---------------------------------------------------------------------------
# SK-draft context — the officer deputy path (draft_sk).
#
# `draft_sk` renders SIAP's OWN PKPP approval-letter template (stereotype
# LICENSE-RECOMMEND, keyed by license_id) filled with what the case holds, then
# hands the editable .docx back to the officer. It needs the licence id (to find
# the template + parse jenis_pengadaan from the name) and the applicant
# identity, none of which the model should have to know — so the bridge injects
# them here per turn, exactly like `_doc_context`. It also reuses `_doc_context`
# for the ONE Vision pass over the submission docs (vessel/permit fields).
#
# Shape: {"license_id": int|None, "ticket": str, "applicant_name": str|None,
#         "alamat": str|None, "license_name": str|None}
# None → no SK context bound (admin-dashboard path); draft_sk says so plainly.
# The applicant fields here go INTO the SK docx (the officer is authorized to
# see them) but are NEVER logged.
# ---------------------------------------------------------------------------
_sk_context: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "officer_copilot_sk_context", default=None
)

# Severity ladder — kept in sync with admin/src/lib/case-types.ts SEVERITY_ORDER
# and ai-engine/services/agents/validator.py IssueLevel. Lower = worse.
_SEVERITY_RANK: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


# ---------------------------------------------------------------------------
# Tool implementations.
# ---------------------------------------------------------------------------


def _documents_digest_from_ctx() -> list[dict[str, Any]]:
    """Return the compact per-document read digest carried on the validation
    context, if any (Task F).

    The bridge derives a `documents_digest` from the rich SuitabilityResult at
    officer-case creation — {filename, detected_type, has_meterai, confidence,
    matches, claimed_type} per doc — and threads it through the same
    `validation` dict the copilot receives, so "what BIMA read per doc" (type +
    meterai) survives a Redis round-trip / process restart even though the raw
    SuitabilityResult is stripped before Redis. We pass it through unchanged;
    the bridge already masked any PII in the fields when it built the digest.
    Returns [] when no digest is bound.
    """
    ctx = _validation_context.get()
    if not ctx or not isinstance(ctx, dict):
        return []
    digest = ctx.get("documents_digest")
    if not isinstance(digest, list):
        return []
    out: list[dict[str, Any]] = []
    for d in digest:
        if not isinstance(d, dict):
            continue
        out.append({
            "filename": d.get("filename", "") or "",
            "detected_type": d.get("detected_type", "") or "",
            "claimed_type": d.get("claimed_type", "") or "",
            "has_meterai": d.get("has_meterai"),
            "confidence": d.get("confidence"),
            "matches": d.get("matches"),
        })
    return out


# Second-person applicant phrasings that must never reach an officer (who is a
# REVIEWER of someone else's packet, not the applicant). Rewritten to the
# neutral "berkas permohonan ini" framing. Case-insensitive; applied to any
# summary prose that flows through get_validation_summary so a validator/bridge
# that still emits citizen-facing wording can't leak "permohonan izin Anda" to
# the officer. Order matters — longer phrases first.
_REVIEWER_REFRAME: tuple[tuple[re.Pattern[str], str], ...] = (
    # Longest/most-specific first so a leading "berkas " isn't left dangling
    # (e.g. "berkas permohonan izin Anda" must not become "berkas berkas ...").
    (re.compile(r"berkas permohonan izin Anda", re.IGNORECASE), "berkas permohonan ini"),
    (re.compile(r"berkas permohonan Anda", re.IGNORECASE), "berkas permohonan ini"),
    (re.compile(r"permohonan izin Anda", re.IGNORECASE), "berkas permohonan ini"),
    (re.compile(r"permohonan Anda", re.IGNORECASE), "berkas permohonan ini"),
    (re.compile(r"berkas Anda", re.IGNORECASE), "berkas permohonan ini"),
    (re.compile(r"dokumen Anda", re.IGNORECASE), "dokumen pemohon"),
    (re.compile(r"izin Anda", re.IGNORECASE), "izin yang dimohonkan"),
)


def _reframe_reviewer(text: str) -> str:
    """Rewrite applicant-facing second-person phrasings ("permohonan izin Anda")
    into the reviewer framing ("berkas permohonan ini"). The officer reviews
    someone else's packet, so these must never address them as the applicant.
    Best-effort text patch on the summary prose; leaves everything else intact."""
    if not text:
        return text
    for pattern, repl in _REVIEWER_REFRAME:
        text = pattern.sub(repl, text)
    return text


def get_validation_summary() -> dict:
    """
    Return the BIMA validator's findings for the case currently in session:
    the completion score and the issue list sorted worst-first.

    This is the copilot's LEAD tool — Decisions §22 makes validation help the
    headline job. The validation result is supplied by admin-api (which runs
    the validator for the case page) and injected via `_validation_context`;
    we never re-run Gemini Vision here.

    Returns a structured dict the model can narrate verbatim:
      {
        "available": bool,
        "score_percent": int,        # 0-100
        "status": str,               # ready | minor_issues | major_issues | unverified
        "summary": str,              # one-paragraph Indonesian
        "issue_count": int,
        "issues": [ {severity, field, message, related_docs}, ... ]  # worst-first
        "documents_read": [ {filename, detected_type, claimed_type,
                             has_meterai, confidence, matches}, ... ]
      }
    `documents_read` is the per-doc digest of what BIMA actually read (type +
    meterai per document) — present even after a restart, so the officer can
    always see it. On miss returns `{"available": False, "note": "..."}` so the
    model is told to ask the officer to run the validator rather than
    fabricating a score.
    """
    ctx = _validation_context.get()
    if not ctx or not isinstance(ctx, dict):
        return {
            "available": False,
            "note": (
                "Hasil validasi belum tersedia untuk tiket ini. Minta petugas "
                "menjalankan validator pada halaman kasus terlebih dahulu."
            ),
        }

    raw_issues = ctx.get("issues") or []
    issues: list[dict[str, Any]] = []
    for it in raw_issues:
        if not isinstance(it, dict):
            continue
        issues.append({
            "severity": str(it.get("severity", "") or "").lower(),
            "field": it.get("field", "") or "",
            "message": it.get("message", "") or "",
            "related_docs": it.get("related_docs", []) or [],
        })
    # Worst-first so the model walks the officer through critical items first.
    issues.sort(key=lambda i: _SEVERITY_RANK.get(i["severity"], 99))

    score_percent = ctx.get("score_percent")
    if score_percent is None and ctx.get("score") is not None:
        try:
            score_percent = int(round(float(ctx["score"]) * 100))
        except (TypeError, ValueError):
            score_percent = None

    return {
        "available": True,
        # The officer is the REVIEWER of someone else's packet, not the
        # applicant — tell the model so it never addresses them as the
        # applicant ("permohonan izin Anda"). See _reframe_reviewer + the
        # officer/signature system prompts.
        "audience": "petugas_peninjau",
        "framing_note": (
            "Ini berkas permohonan PEMOHON yang sedang ditinjau petugas. "
            "Sampaikan sebagai peninjauan 'berkas permohonan ini' / 'berkas "
            "pemohon' — JANGAN sapa petugas sebagai pemohon dan JANGAN gunakan "
            "'permohonan izin Anda' atau 'berkas Anda'."
        ),
        "score_percent": score_percent if score_percent is not None else 0,
        "status": ctx.get("status", "unverified") or "unverified",
        "summary": _reframe_reviewer(ctx.get("summary", "") or ""),
        "issue_count": len(issues),
        "issues": issues,
        "documents_read": _documents_digest_from_ctx(),
    }


async def get_case_full(ticket: str) -> dict:
    """
    Fetch the SIAP monitoring-berkas record for a ticket. Reuses the same
    client the WhatsApp fast-path uses, so the auth + timeout story is
    identical.

    Also attaches `documents_read` — the compact per-doc digest of what BIMA
    read at submission (type + meterai per document, Task F) — from the
    validation context, so the officer sees it alongside the case record even
    after a restart. Absent (empty list) when no digest is bound.

    Returns the record dict on hit, or `{"found": False, "ticket": ticket}`
    on miss / SIAP unavailable. We do not raise — the model must always
    get a structured tool result so it can phrase the answer correctly.
    """
    digest = _documents_digest_from_ctx()
    client = get_siap_client()
    record = await client.get_status_by_ticket(ticket)
    if not record:
        return {"found": False, "ticket": ticket, "documents_read": digest}
    return {"found": True, **record, "documents_read": digest}


# Gemini Vision schema for a free-text document summary. JSON-mode (the
# vision client only returns JSON), so we wrap the prose in a `summary` field.
_DOC_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "A concise Indonesian summary (3-5 sentences) of what this "
                "document is and the key information it contains."
            ),
        },
    },
    "required": ["summary"],
}

_DOC_SUMMARY_PROMPT = (
    "Anda asisten petugas perizinan. Ringkas dokumen terlampir dalam Bahasa "
    "Indonesia: jenis dokumen, pihak/nama yang tercantum, poin penting "
    "(nomor, tanggal, materai bila ada), dan apakah dokumen tampak lengkap. "
    "Jangan mengarang isi yang tidak terlihat. 3-5 kalimat."
)


def _digest_summary_for_ref(doc_ref: str) -> Optional[str]:
    """Best-effort summary of a document from the RETAINED read-digest text
    when its bytes are gone (post-restart / Redis rehydrate stripped them).

    The digest (Task F) survives a Redis round-trip while the raw bytes may not,
    so "what BIMA read" (detected type, meterai, confidence) is still available
    even when a Vision re-pass is impossible. We match `doc_ref` against the
    digest's detected/claimed type + filename (same tolerance as
    `_resolve_doc_ref`) and phrase a short factual line from it — never a
    dead-end. Returns None when no digest entry matches.
    """
    digest = _documents_digest_from_ctx()
    if not digest:
        return None

    match: Optional[dict[str, Any]] = None
    if _norm_ref(doc_ref):
        # Alias-aware match (shared with the send fallback) so "gambar rancang
        # bangun kapal" summarises the "Desain_Kapal" digest entry too.
        for entry in digest:
            if _digest_entry_matches_ref(entry, doc_ref):
                match = entry
                break
    # A single-document submission: honour a summary request even without a
    # crisp label match, so the officer still gets what BIMA read.
    if match is None and len(digest) == 1:
        match = digest[0]
    if match is None:
        return None

    detected = str(match.get("detected_type") or "").strip()
    filename = str(match.get("filename") or "").strip()
    label = filename or detected or doc_ref
    bits: list[str] = []
    if detected:
        bits.append(f"BIMA membacanya sebagai *{detected}*")
    conf = match.get("confidence")
    if isinstance(conf, (int, float)):
        bits.append(f"keyakinan {int(round(float(conf) * 100))}%")
    if match.get("has_meterai") is True:
        bits.append("materai terdeteksi")
    elif match.get("has_meterai") is False:
        bits.append("materai tidak terdeteksi")
    if match.get("matches") is True:
        bits.append("jenis sesuai dengan yang dinyatakan pemohon")
    elif match.get("matches") is False:
        bits.append("jenis TIDAK cocok dengan label pemohon")
    detail = "; ".join(bits) if bits else "hanya metadata dasar yang tersimpan"
    return (
        f"📄 {label}: berkas aslinya tidak lagi tersedia di sesi ini, jadi "
        f"saya belum bisa membaca ulang isinya. Dari catatan pemeriksaan: "
        f"{detail}."
    )


async def get_doc_summary(doc_ref: str = "", file_id: str = "") -> str:
    """
    Return a short Indonesian summary of one supporting document. `doc_ref`
    may be a document TYPE/NAME ("KTP", "surat permohonan", "proposal") OR a
    literal file_id — the officer speaks in plain names, not ids. `file_id` is
    accepted as an alias for backward-compat (the tool's earlier signature) so
    a model that still emits `file_id` keeps working.

    DEMO PATH (chat bridge): when the citizen sent the document into the
    WhatsApp/Telegram session, BIMA holds the raw bytes and the officer bridge
    injects them via `_doc_context`. We resolve the ref to a file_id, run
    Gemini Vision over the in-session bytes, and return a real summary — a
    genuine answer to "apa isi proposalnya?".

    GRACEFUL DEGRADE: if the bytes are genuinely gone (post-restart rehydrate
    dropped them) but BIMA still holds the compact read-digest, we summarise
    from that digest text and SAY the original can't be re-read — never a
    dead-end on "tidak memiliki ID file".

    FALLBACK (admin dashboard): when neither bytes nor digest are bound (SIAP
    has no file-download endpoint), we return the canned placeholder.
    """
    doc_ref = (doc_ref or file_id or "").strip()
    ctx = _doc_context.get()
    if ctx and isinstance(ctx, dict):
        file_id = _resolve_doc_ref(ctx, doc_ref)
        doc = ctx.get(file_id) if file_id else None
        content = doc.get("content") if doc else None
        if doc is not None and content:
            from services.gemini_vision import extract_structured, is_configured

            if not is_configured():
                return (
                    f"Ringkasan dokumen {doc.get('filename', file_id)} belum "
                    "dapat dibuat — Gemini Vision belum dikonfigurasi di server."
                )
            parsed = await extract_structured(
                image_bytes=content,
                mime_type=doc.get("mime_type", "application/octet-stream"),
                prompt=_DOC_SUMMARY_PROMPT,
                response_schema=_DOC_SUMMARY_SCHEMA,
            )
            if parsed and isinstance(parsed, dict) and parsed.get("summary"):
                label = doc.get("filename", file_id)
                return f"📄 {label}: {str(parsed['summary']).strip()}"
            # Vision failed on real bytes — try the digest before giving up.
            degraded = _digest_summary_for_ref(doc_ref)
            if degraded:
                return degraded
            return (
                f"Tidak dapat membaca dokumen {doc.get('filename', file_id)} — "
                "pastikan berkas jelas (foto/PDF tidak buram)."
            )

        # Resolved to a doc whose bytes are gone (or didn't resolve at all):
        # degrade to the retained digest rather than dead-ending on "no id".
        degraded = _digest_summary_for_ref(doc_ref)
        if degraded:
            return degraded

        # Nothing to summarise from — tell the model what IS available so it can
        # re-ask, listing human labels (not opaque ids) where we have them.
        labels = sorted(
            (
                d.get("filename")
                or d.get("detected_type")
                or d.get("claimed_type")
                or fid
            )
            for fid, d in ctx.items()
        )
        available = ", ".join(labels) or "(tidak ada)"
        return (
            f"Dokumen '{doc_ref}' tidak ditemukan di sesi ini. "
            f"Dokumen yang tersedia: {available}."
        )

    # No in-session bytes bound — try the retained digest (survives a restart
    # even when the doc context does not), then the canned placeholder.
    degraded = _digest_summary_for_ref(doc_ref)
    if degraded:
        return degraded
    return (
        f"[Ringkasan dokumen '{doc_ref}' belum tersedia di jalur ini]. "
        "Endpoint pengunduhan dokumen SIAP belum diaktifkan. Pada alur chat "
        "(WhatsApp/Telegram) di mana dokumen dikirim langsung ke BIMA, "
        "ringkasan dibuat otomatis dengan Gemini Vision."
    )


# File extensions stripped off a filename label before matching, so the
# officer's "desain kapal" resolves the upload "Desain_Kapal.pdf" (the trailing
# ".pdf" would otherwise block an exact/whole-word match). Kept to the document
# types SIAP accepts for an APPLICANT-UPLOAD.
_DOC_EXTENSIONS = re.compile(r"\.(pdf|jpe?g|png|docx?|rtf|tiff?|webp|heic)$", re.IGNORECASE)


def _norm_ref(s: Any) -> str:
    """Space/underscore-agnostic, extension-stripped, case-insensitive label
    normaliser. Shared by the doc-ref resolver and the digest fallback so
    "surat pesanan", "Surat_Pesanan", "SURAT  PESANAN", and the filename
    "Desain_Kapal.pdf" all collapse to one comparable form (the last dropping
    its ".pdf" so a request by NAME resolves the FILE)."""
    s = _DOC_EXTENSIONS.sub("", str(s or ""))
    return re.sub(r"[\s_]+", " ", s).strip().lower()


# Document-reference ALIAS map (rehearsal fix). An officer says "gambar rancang
# bangun kapal" or "desain kapal" for the same upload SIAP labels "Desain_Kapal"
# / the requirement calls "Desain Kapal"; "NIB" and "izin usaha" both mean the
# SIUP upload; and so on. Each entry maps a canonical token → the phrases that
# mean it. We canonicalise BOTH the officer's ref and each doc label to this
# token before comparing, so a request by REQUIREMENT LABEL or by FILENAME both
# resolve regardless of the exact words. Longest phrases first per row so a
# specific phrase wins over a shorter substring of it.
_DOC_ALIAS_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("desain_kapal", (
        "gambar rancang bangun kapal", "gambar rancang bangun",
        "rancang bangun kapal", "rancang bangun", "desain kapal", "desain",
    )),
    ("siup", (
        "siup oss", "surat izin usaha perikanan", "izin usaha perikanan",
        "izin usaha", "siup", "nib",
    )),
    ("surat_permohonan", ("surat permohonan", "permohonan")),
    # No bare "api" alias — as a 3-char phrase it substring-matched common
    # Indonesian words (Rekapitulasi, Persiapan, rapi…) via `_canonical_doc_key`
    # and could mis-resolve the doc-view to the spesifikasi doc. The multi-word
    # phrases carry the real signal.
    ("spesifikasi_alat_tangkap", (
        "spesifikasi alat tangkap", "spesifikasi", "alat tangkap",
    )),
    ("persetujuan_nama_kapal", (
        "persetujuan nama kapal", "nama kapal", "srt kapal", "surat kapal",
    )),
    ("pakta_integritas", ("pakta integritas", "pakta")),
    ("surat_pesanan", ("surat pesanan", "kontrak", "pesanan")),
    ("ktp", ("kartu tanda penduduk", "ktp")),
)


def _canonical_doc_key(s: Any) -> Optional[str]:
    """Canonicalise a normalised label/ref to its alias token, or None.

    Returns the canonical token (e.g. "desain_kapal") when the normalised text
    CONTAINS one of that token's alias phrases — so "gambar rancang bangun
    kapal", "desain kapal", and the filename "Desain_Kapal.pdf" all canonicalise
    to "desain_kapal", making the officer's synonym resolve the actual upload.
    None when no alias group matches (the caller then falls back to the plain
    normalised match)."""
    norm = _norm_ref(s)
    if not norm:
        return None
    for canon, phrases in _DOC_ALIAS_GROUPS:
        for phrase in phrases:
            if phrase and phrase in norm:
                return canon
    return None


def _resolve_doc_ref(ctx: dict, doc_ref: str) -> Optional[str]:
    """Resolve a human `doc_ref` ("KTP", "Surat Pesanan", "gambar rancang bangun
    kapal", a filename fragment) OR a literal file_id to a file_id present in the
    in-session doc context.

    Matching is case-insensitive, extension-stripped, and tolerant of the label
    formatting the validator uses (underscores in detected/claimed types, e.g.
    "Surat_Pesanan" vs the officer's "surat pesanan") AND of SYNONYMS (via the
    `_DOC_ALIAS_GROUPS` map — "desain kapal" / "gambar rancang bangun kapal" /
    "rancang bangun" all resolve the same "Desain_Kapal" upload; "NIB" / "izin
    usaha" resolve the SIUP upload). Precedence (most specific → least):
      1. exact file_id key,
      2. ALIAS match — the ref and a doc label (detected/claimed/filename)
         canonicalise to the SAME alias token. This is the rehearsal fix: a
         request by requirement LABEL or by FILENAME both resolve.
      3. exact type-label equality — detected_type (what BIMA READ the doc to
         be) OR claimed_type (what the citizen DECLARED) equals the ref. Detected
         is tried first: "KTP" must resolve even when the citizen mislabelled the
         upload, which is exactly a case the officer is reviewing.
      4. filename substring, then type-label substring (detected then claimed).
    Returns the file_id on a hit, or None when nothing matches.

    `detected_type` is present when the bridge cross-referenced the read digest
    onto the doc context; it may be absent on the admin-dashboard path — the
    resolver simply skips empty labels, so it degrades to claimed_type+filename.
    """
    if not ctx or not doc_ref:
        return None

    ref = _norm_ref(doc_ref)

    # 1) A literal file_id the officer (or model) passed straight through.
    if doc_ref in ctx:
        return doc_ref
    if not ref:
        return None

    # 2) ALIAS match (rehearsal fix). Canonicalise the ref; if it names a known
    #    document, resolve the doc whose detected/claimed/filename canonicalises
    #    to the SAME token — so "gambar rancang bangun kapal" and "desain kapal"
    #    both find "Desain_Kapal.pdf" whether the doc is labelled by requirement
    #    or only carries a filename. Filename first (most specific), then labels.
    ref_canon = _canonical_doc_key(doc_ref)
    if ref_canon is not None:
        for fid, doc in ctx.items():
            if _canonical_doc_key(doc.get("filename")) == ref_canon:
                return fid
        for label_key in ("detected_type", "claimed_type"):
            for fid, doc in ctx.items():
                if _canonical_doc_key(doc.get(label_key)) == ref_canon:
                    return fid

    # 3) Exact type-label equality — detected first (BIMA's read), then claimed.
    for label_key in ("detected_type", "claimed_type"):
        for fid, doc in ctx.items():
            if _norm_ref(doc.get(label_key)) == ref:
                return fid

    # 4) Substring match — filename first (most specific), then type labels.
    for fid, doc in ctx.items():
        if ref in _norm_ref(doc.get("filename")):
            return fid
    for label_key in ("detected_type", "claimed_type"):
        for fid, doc in ctx.items():
            label = _norm_ref(doc.get(label_key))
            if label and (ref in label or label in ref):
                return fid

    return None


def _digest_entry_matches_ref(entry: dict, doc_ref: str) -> bool:
    """True if a read-digest entry matches `doc_ref` — by alias synonym OR by
    the plain normalised label overlap. Shared by the digest send + summary
    fallbacks so both agree on what documents exist, and both honour the same
    synonym map as `_resolve_doc_ref` (so "gambar rancang bangun kapal" matches
    the "Desain_Kapal" digest entry too)."""
    ref = _norm_ref(doc_ref)
    if not ref:
        return False
    ref_canon = _canonical_doc_key(doc_ref)
    if ref_canon is not None:
        for key in ("detected_type", "claimed_type", "filename"):
            if _canonical_doc_key(entry.get(key)) == ref_canon:
                return True
    labels = (
        _norm_ref(entry.get("detected_type")),
        _norm_ref(entry.get("claimed_type")),
        _norm_ref(entry.get("filename")),
    )
    return any(lbl and (ref == lbl or ref in lbl or lbl in ref) for lbl in labels)


def _bytes_gone_message_for_ref(doc_ref: str) -> Optional[str]:
    """When a send/view ref doesn't resolve in the (bytes-carrying) doc context
    but DOES match the retained read-digest, return an honest "we read it, the
    file is gone" message. Returns None when the digest doesn't know the ref
    either (a genuine not-found). Mirrors `_digest_summary_for_ref`'s matching
    so send + summary agree on what documents exist."""
    digest = _documents_digest_from_ctx()
    if not digest:
        return None
    if not _norm_ref(doc_ref):
        return None
    for entry in digest:
        if _digest_entry_matches_ref(entry, doc_ref):
            label = (
                str(entry.get("filename") or "").strip()
                or str(entry.get("detected_type") or "").strip()
                or doc_ref
            )
            return (
                f"Dokumen {label} tercatat pada pengajuan ini, tetapi berkas "
                "aslinya tidak lagi tersimpan di sesi (mis. setelah proses "
                "dimulai ulang), jadi belum bisa saya kirimkan."
            )
    return None


def send_document(doc_ref: str) -> str:
    """Officer asks to RECEIVE/VIEW a specific uploaded document ("kirim
    KTP-nya", "boleh saya lihat surat pesanannya").

    This tool does NOT send the file — a tool can only return text, and the
    delivery is an out-of-band side effect. It resolves `doc_ref` (a document
    label like "KTP"/"Surat Pesanan", OR a file_id) against the in-session
    documents, records the resolved file_id in `_docs_to_send_context` so the
    officer bridge can push the real file on the officer's channel, and returns
    a short confirmation for the model to relay. On no match it records nothing
    and tells the officer the document isn't on this submission.
    """
    ctx = _doc_context.get()
    if not ctx or not isinstance(ctx, dict):
        # No in-session bytes bound. It may still be a doc BIMA read whose bytes
        # were dropped on a rehydrate — the retained digest remembers it; give
        # the truthful "read it, file gone" line rather than a blanket "not
        # available on this session". Falls back to the blanket line when even
        # the digest doesn't know the ref (genuine admin-dashboard path).
        degraded = _bytes_gone_message_for_ref(doc_ref)
        if degraded:
            return degraded
        return (
            f"Dokumen '{doc_ref}' tidak dapat dikirim: berkas tidak tersedia "
            "pada sesi ini."
        )

    file_id = _resolve_doc_ref(ctx, doc_ref)
    if file_id is None:
        # Not in the (bytes-carrying) doc context. It may still be a document
        # BIMA read whose bytes were dropped on a Redis rehydrate — the digest
        # remembers it. Distinguish "we never had it" from "we had it, bytes
        # gone" so the officer gets a truthful, non-dead-end answer.
        degraded = _bytes_gone_message_for_ref(doc_ref)
        if degraded:
            return degraded
        return f"Dokumen '{doc_ref}' tidak ditemukan pada pengajuan ini."

    doc = ctx.get(file_id) or {}
    label = doc.get("filename") or doc.get("claimed_type") or file_id

    # The doc resolved but its bytes are gone (empty content after a rehydrate
    # that dropped the payload — e.g. an oversize doc not persisted to Redis).
    # A tool can only return text, and _deliver_documents would silently skip an
    # empty doc; tell the officer plainly instead of confirming a send that
    # never happens.
    if not doc.get("content"):
        return (
            f"Dokumen {label} tercatat pada pengajuan ini, tetapi berkas "
            "aslinya tidak lagi tersimpan di sesi (mis. setelah proses "
            "dimulai ulang), jadi belum bisa saya kirimkan. Ringkasannya "
            "masih bisa saya sampaikan dari catatan pemeriksaan."
        )

    queue = _docs_to_send_context.get()
    if isinstance(queue, list):
        if file_id not in queue:
            queue.append(file_id)
    else:
        # No queue bound (e.g. tool exercised in isolation) — the resolution
        # still succeeds; only the out-of-band send is skipped.
        logger.debug("send_document resolved but no send queue bound | file_id=%s", file_id)

    return f"Baik, saya kirimkan dokumen {label} ke chat ini."


# Human labels for the SK placeholder keys, so the narration reads naturally.
_SK_FIELD_LABELS: dict[str, str] = {
    "nama_pemohon": "Nama Pemohon",
    "alamat": "Alamat",
    "jenis_pengadaan": "Jenis Pengadaan",
    "nama_kapal": "Nama Kapal",
    "gt": "GT",
    "bahan": "Bahan Kapal",
    "thn_bangun": "Tahun Bangun",
    "alat": "Alat Tangkap",
    "galangan": "Galangan",
    "no_siup": "No. SIUP",
    "tgl_siup": "Tgl. SIUP",
    "tgl_srt_permohonan": "Tgl. Surat Permohonan",
    "perihal_surat_perm": "Perihal Surat",
    "tipe": "Tipe Kapal",
    "no_ppkp": "No. PPKP",
    "rekom_tgl_penetapan": "Tgl. Penetapan",
}

# All 16 keys in template order, so we can report filled-vs-blank exhaustively.
_SK_ALL_KEYS: tuple[str, ...] = (
    "nama_pemohon", "alamat", "jenis_pengadaan",
    "nama_kapal", "gt", "bahan", "thn_bangun", "alat", "galangan",
    "no_siup", "tgl_siup", "tgl_srt_permohonan", "perihal_surat_perm", "tipe",
    "no_ppkp", "rekom_tgl_penetapan",
)


# Pretty-print a placeholder key for the officer's filled/blank note when we
# have no curated label (generic path — ANY licence). Uses the curated PKPP
# labels when present, else Title-Cases the snake_case key.
def _pretty_field_label(key: str) -> str:
    if key in _SK_FIELD_LABELS:
        return _SK_FIELD_LABELS[key]
    return (key or "").replace("_", " ").strip().title() or key


# ---------------------------------------------------------------------------
# DRAFT GATE — the SK is generated by SIAP FROM the filled Formulir Isian, so a
# draft is meaningless while that form is empty. When we can DISCOVER the
# applicant form for this licence (form_id resolves) and its form_value is empty
# OR every vessel-subject field is blank, draft_sk REFUSES and guides the officer
# to complete the Formulir Isian first. When no applicant form is discoverable
# (form_id is None), we DO NOT gate — other licences keep the current behaviour.
#
# _GATE_REQUIRED_KEYS — the subject/applicant fields a meaningful draft needs
#   (the fields that were blank in the rehearsal). Listed to the officer.
# _GATE_VESSEL_KEYS   — the subset whose all-blank/absent state (together with an
#   empty form) triggers the refusal. These are the vessel-subject fields the SK
#   is fundamentally ABOUT; if none is present the form is effectively unfilled.
#
# SCOPING (deliberate): these keys are PKPP-shaped, because PKPP (license 459,
# form 560) is the ONLY licence wired for the copilot form-fill / SK-draft
# machinery today, and the requirement is precisely "refuse the draft while the
# vessel data is blank" (a partially-filled header alone → an empty SK). If/when
# this copilot expands to a NON-vessel sector whose applicant form also matches
# get_applicant_form_id, revisit: gate on "the form_value carries no non-blank
# value among its OWN keys" and keep _GATE_REQUIRED_KEYS only for the blank-field
# hint text — otherwise a fully-filled non-vessel form would be wrongly blocked.
# ---------------------------------------------------------------------------
_GATE_REQUIRED_KEYS: tuple[str, ...] = (
    "nama_pemohon", "alamat", "nama_kapal", "thn_bangun", "tipe",
    "alat", "bahan", "gt", "galangan",
)
_GATE_VESSEL_KEYS: tuple[str, ...] = (
    "nama_kapal", "thn_bangun", "tipe", "alat", "bahan", "gt", "galangan",
)


def _form_value_key_blank(fv: dict, key: str) -> bool:
    """True when a form_value key is absent or its value is blank/whitespace."""
    if not fv or not isinstance(fv, dict) or key not in fv:
        return True
    return not str(fv.get(key) or "").strip()


def _gate_blocks_draft(fv: dict) -> bool:
    """The DRAFT GATE predicate: refuse the draft when the filled Formulir Isian
    is empty OR every vessel-subject key is blank/absent. Only consulted when the
    applicant form_id was resolvable."""
    if not fv or not isinstance(fv, dict):
        return True
    return all(_form_value_key_blank(fv, k) for k in _GATE_VESSEL_KEYS)


async def draft_sk() -> str:
    """DRAFT the correct SIAP output document (Rekomendasi Teknis or Surat
    Keputusan) for the case in session AT ITS CURRENT STEP, using SIAP's OWN
    template, filled from REAL data, and send it to the officer as a PDF (to
    read) + editable .docx (to edit).

    This is a DRAFTING aid, not a SIAP write — SIAP issues the final SK ber-TTE
    at approval. So it is NOT confirmation-gated.

    GENERALIZED (task #80) — works for ANY licence, not just PKPP:
      1. PER-STEP: choose the output stereotype the CURRENT desk calls for
         (LICENSE-RECOMMEND at the technical/review desk, LICENSE-SK at the
         signing desk) via siap_get_step_output_template; fall back to whichever
         fillable template the licence has and SAY so.
      2. DISCOVER the fetched template's `[data.KEY]` keys dynamically.
      3. FILL each key from a REAL source — person_profile.properties (via the
         request profile_id), the license_request/license/step case meta, or ONE
         best-effort Vision pass over the submission docs. Anything unresolved
         (SIAP-issued number/date, or simply unreadable) is LEFT BLANK — never
         guessed.
      4. Report filled-vs-blank so the officer completes the blanks.

    Degrades gracefully (no license_id / template missing / Vision fails /
    python-docx absent) to an honest message — never raises, never a dead-end.
    """
    sk = _sk_context.get()
    if not sk or not isinstance(sk, dict):
        return (
            "Draf dokumen belum bisa dibuat di jalur ini: konteks berkas (izin & "
            "pemohon) tidak tersedia pada sesi. Pada alur chat petugas, draf "
            "dibuat otomatis dari template resmi SIAP."
        )
    license_id = sk.get("license_id")
    if not license_id:
        return (
            "Draf dokumen belum bisa dibuat: ID izin (license_id) tidak "
            "diketahui untuk berkas ini, sehingga template resmi SIAP tidak "
            "dapat ditemukan. Silakan terbitkan dokumen langsung di SIAP."
        )

    try:
        from services import siap_templates as st
        from services import siap_tools as stools
        from services import siap_db as sdb
    except Exception:  # pragma: no cover — module import guard
        return (
            "Draf dokumen belum bisa dibuat: modul template dokumen tidak "
            "tersedia di server ini."
        )

    documents = _doc_context.get() or {}
    ticket = sk.get("ticket")
    is_final_step = bool(sk.get("is_final_step"))

    try:
        # 1) PER-STEP output-template selection (Rekomtek vs SK by the desk).
        tmpl = await stools.siap_get_step_output_template(
            int(license_id),
            is_final_step=is_final_step,
            step_stereotype=sk.get("step_stereotype"),
        )
    except Exception:
        logger.exception("draft_sk: step-template lookup failed | license_id=%s", license_id)
        tmpl = {"found": False}

    if not tmpl.get("found"):
        return (
            "Template dokumen resmi SIAP untuk izin ini belum tersedia/terunduh, "
            "jadi draf otomatis belum bisa dibuat. Dokumen final tetap "
            "diterbitkan oleh SIAP saat berkas disetujui."
        )

    internal = tmpl.get("internal_filename")
    doc_kind = tmpl.get("doc_kind") or "dokumen"
    doc_label = {"sk": "Surat Keputusan (SK)", "rekomendasi": "Rekomendasi Teknis"}.get(
        doc_kind, "dokumen"
    )
    out_prefix = {"sk": "SK", "rekomendasi": "Rekomtek"}.get(doc_kind, "Dokumen")
    if not internal or not str(internal).lower().endswith(".docx"):
        # A template exists but is not a fillable .docx (legacy .doc/.rtf or the
        # ${key} SK docx BIMA can't fill). Be honest rather than draft nothing.
        return (
            f"Template {doc_label} untuk izin ini ada di SIAP, tetapi belum "
            "dalam format yang bisa BIMA isi otomatis (bukan .docx isian). "
            "Silakan susun/terbitkan dokumen langsung di SIAP."
        )

    # 2) Fetch REAL data handles (all read-only, all degrade to empty).
    #    Resolve the case meta FIRST so the applicant profile_id can fall back
    #    to it: a durable/rehydrated officer session may carry no profile_id (or
    #    one encoded before the request was profile-linked), and reading the
    #    profile off a stale sk_context alone would leave nama_pemohon/alamat
    #    blank on the draft even though SIAP has them on the linked profile.
    case_meta: dict = {}
    license_name = sk.get("license_name")
    request_id = None
    try:
        # Prefer the ticket-scoped case meta so ticket/step are authoritative.
        if ticket:
            request_id = await stools.siap_resolve_request_id(str(ticket))
        if request_id is not None:
            case_meta = await sdb.get_request_case_meta(int(request_id))
        if not license_name:
            license_name = await sdb.get_license_name(int(license_id))
    except Exception:
        logger.exception("draft_sk: case-meta read failed | license_id=%s", license_id)

    profile: dict = {}
    profile_id = sk.get("profile_id") or (case_meta.get("profile_id") if case_meta else None)
    if profile_id is not None:
        try:
            profile = await sdb.get_person_profile_properties(int(profile_id))
        except Exception:
            logger.exception("draft_sk: profile read failed | profile_id set")
            profile = {}

    # 2b) DRAFT GATE + form-sourcing. The SK is generated by SIAP FROM the filled
    #     Formulir Isian, so a draft is meaningless while that form is empty. If
    #     we can DISCOVER the applicant form (form_id resolves), read its
    #     form_value and:
    #       - REFUSE (produce no draft, send no document) when the form is empty
    #         OR every vessel-subject field is blank — guide the officer to fill
    #         the Formulir Isian first.
    #       - otherwise pass the form_value as `overrides` so the draft reflects
    #         EXACTLY what the SIAP form holds (SOURCE_FORM wins per key; keys the
    #         form lacks still fall back to profile/case/Vision/blank).
    #     If NO applicant form is discoverable (form_id None) we DO NOT gate — the
    #     current behaviour is preserved so other licences are not regressed.
    form_id: Optional[int] = None
    form_value: Optional[dict] = {}
    if request_id is not None:
        try:
            form_id = await sdb.get_applicant_form_id(int(license_id))
        except Exception:
            logger.exception(
                "draft_sk: form_id resolve failed | license_id=%s", license_id
            )
            form_id = None
        if form_id is not None:
            try:
                form_value = await sdb.get_form_value_properties(
                    int(form_id), int(request_id)
                )
            except Exception:
                # Could not read → treat as "unknown" (None), NOT empty, so a
                # transient failure degrades to the ungated draft below.
                logger.exception(
                    "draft_sk: form_value read failed | form_id=%s request_id=%s",
                    form_id, request_id,
                )
                form_value = None
            # form_value is None ONLY when the read could not be completed (a
            # transient DB error). We cannot tell whether the form is filled, so
            # DO NOT gate — degrade to the ungated draft (as when no applicant
            # form exists). A genuinely empty/missing form returns {} and IS gated.
            if form_value is not None and _gate_blocks_draft(form_value):
                # REFUSE — the officer must complete the Formulir Isian first.
                blank_required = [
                    k for k in _GATE_REQUIRED_KEYS
                    if _form_value_key_blank(form_value, k)
                ]
                blank_labels = ", ".join(
                    _pretty_field_label(k) for k in blank_required
                ) or "(seluruh data kapal & pemohon)"
                logger.info(
                    "draft_sk GATE refuse | license_id=%s form_id=%s "
                    "request_id=%s blank_required=%d",
                    license_id, form_id, request_id, len(blank_required),
                )
                return (
                    "Draf belum bisa dibuat: Formulir Isian permohonan ini masih "
                    "kosong di SIAP. SK diterbitkan SIAP DARI Formulir Isian itu, "
                    "jadi draf tidak dapat disusun selama formulir belum "
                    "dilengkapi. Mohon lengkapi dulu Formulir Isian di SIAP — "
                    f"field yang masih kosong: {blank_labels}. Setelah Formulir "
                    "Isian terisi (mis. lewat perintah 'isi Formulir Isian'), "
                    "saya bisa membuat drafnya. "
                    + _siap_edit_link_line(request_id)
                )

    # 2c) MERGED form-value overrides. The draft must reflect fields that live on
    #     DIFFERENT forms of this licence — the applicant Formulir Isian (560:
    #     vessel/identity) AND the officer Penomoran form (768: no_ppkp the officer
    #     assigned). get_all_form_values merges every bound form's form_value into
    #     one dict so no_ppkp lands in the Rekomtek/SK draft. It NEVER raises and
    #     returns {} on a miss/transient error, in which case we fall back to the
    #     applicant form_value already read for the gate — so a transient read
    #     still degrades safely and no field is lost. The GATE above is unchanged
    #     (it keys on the applicant vessel fields; 768 adds no gate key).
    overrides: Optional[dict] = None
    if request_id is not None:
        merged: dict = {}
        try:
            merged = await sdb.get_all_form_values(int(license_id), int(request_id))
        except Exception:
            logger.exception(
                "draft_sk: merged form-values read failed | license_id=%s "
                "request_id=%s", license_id, request_id,
            )
            merged = {}
        # Prefer the merged 560+768 values; fall back to the applicant-only
        # form_value (read for the gate) when the merge yielded nothing.
        overrides = merged or (form_value or None)
    else:
        overrides = form_value or None

    # 3) GENERIC render — discover keys, resolve from real sources, fill.
    #    The filled Formulir Isian (form_value) is the SOURCE OF TRUTH: passed as
    #    `overrides` so each key it holds wins (SOURCE_FORM) with no divergent
    #    re-read; keys it lacks still fall back to profile/case/Vision/blank.
    try:
        rendered = await st.render_output_docx(
            internal_filename=internal,
            profile=profile,
            case_meta=case_meta or ({"ticket": ticket} if ticket else None),
            license_name=license_name,
            documents=documents,
            ticket=ticket,
            out_prefix=out_prefix,
            overrides=overrides,
        )
    except Exception:
        logger.exception("draft_sk: render failed | license_id=%s", license_id)
        return (
            "Maaf, terjadi kendala saat menyusun draf dokumen. Dokumen resmi "
            "tetap dapat diterbitkan langsung di SIAP saat berkas disetujui."
        )

    if rendered is None:
        return (
            f"Template {doc_label} resmi SIAP untuk izin ini belum "
            "tersedia/terunduh, jadi draf otomatis belum bisa dibuat. Dokumen "
            "final tetap diterbitkan oleh SIAP saat berkas disetujui."
        )

    docx_bytes, filename, data, classes = rendered

    # 4) Filled vs blank — computed from the DISCOVERED keys (any licence), not
    # a fixed 16-key list. A key present in `classes` but with no value in
    # `data` is a blank the officer must complete.
    #
    # H1 — separate provenance in the note. A PROFILE/CASE fill is a
    # DETERMINISTIC read from SIAP's own DB (authoritative). A VISION fill was
    # OCR'd off a citizen-uploaded document (probabilistic) — the officer MUST
    # eyeball it. We NEVER present a Vision read as an authoritative DB fact, so
    # the two are reported in distinct sentences.
    all_keys = list(classes.keys())
    filled = [k for k in all_keys if str(data.get(k) or "").strip()]
    blank = [k for k in all_keys if not str(data.get(k) or "").strip()]
    filled_official = [
        k for k in filled
        if classes.get(k) in (st.SOURCE_PROFILE, st.SOURCE_CASE)
    ]
    filled_vision = [k for k in filled if classes.get(k) == st.SOURCE_VISION]

    # Queue the rendered .docx for out-of-band delivery on the officer channel.
    queue = _docs_to_send_context.get()
    if isinstance(queue, list):
        queue.append({
            "filename": filename,
            "content": docx_bytes,
            "mime_type": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        })
    else:  # pragma: no cover — tool exercised without a bound queue
        logger.debug("draft_sk rendered but no send queue bound")

    # ALSO produce a read-only PDF of the SAME filled content and queue it, so
    # the officer can PREVIEW the draft inline on WhatsApp — .docx does not
    # preview reliably there. Pure-Python (fpdf2); best-effort.
    pdf_sent = False
    try:
        pdf_view = st.render_pdf_view_from_docx(docx_bytes, ticket=ticket)
    except Exception:  # pragma: no cover — render_pdf_view already guards
        logger.exception("draft_sk: PDF view render raised | license_id=%s", license_id)
        pdf_view = None
    if pdf_view is not None and isinstance(queue, list):
        pdf_bytes, _pdf_name = pdf_view
        # Name the PDF after the docx so the pair reads as one document.
        pdf_name = filename.rsplit(".", 1)[0] + ".pdf"
        queue.append({
            "filename": pdf_name,
            "content": pdf_bytes,
            "mime_type": "application/pdf",
        })
        pdf_sent = True

    official_labels = ", ".join(_pretty_field_label(k) for k in filled_official) or "(tidak ada)"
    vision_labels = ", ".join(_pretty_field_label(k) for k in filled_vision) or "(tidak ada)"
    blank_labels = ", ".join(_pretty_field_label(k) for k in blank) or "(tidak ada)"
    logger.info(
        "draft_sk | license_id=%s | kind=%s fell_back=%s | keys=%d filled=%d "
        "(official=%d vision=%d) blank=%d | pdf_view=%s",
        license_id, doc_kind, tmpl.get("fell_back"), len(all_keys),
        len(filled), len(filled_official), len(filled_vision), len(blank),
        pdf_sent,
    )

    # If the step called for a different stereotype than what we could fill,
    # tell the officer honestly which document was drafted.
    fallback_note = ""
    if tmpl.get("fell_back"):
        want = "SK" if tmpl.get("preferred_stereotype") == "LICENSE-SK" else "Rekomendasi Teknis"
        fallback_note = (
            f"Catatan: tahap ini sebetulnya menghasilkan {want}, namun template "
            f"yang bisa BIMA isi otomatis adalah {doc_label} — itulah yang saya "
            "draftkan. "
        )

    delivery = (
        f"Draf {doc_label} sudah saya siapkan dari template resmi SIAP dan saya "
        f"kirim dalam dua berkas: PDF untuk dibaca/dipratinjau ({filename.rsplit('.', 1)[0]}"
        f".pdf) dan .docx untuk diedit ({filename}). "
        if pdf_sent
        else (
            f"Draf {doc_label} sudah saya siapkan dari template resmi SIAP dan "
            f"saya kirim sebagai berkas .docx yang bisa diedit ({filename}). Bila "
            ".docx sulit dibuka, unduh lalu buka di Word / Google Docs / WPS. "
        )
    )
    return (
        fallback_note
        + delivery
        + f"Terisi dari data resmi SIAP (profil pemohon & data permohonan): "
        f"{official_labels}. "
        + f"Dibaca otomatis dari dokumen unggahan — MOHON DIPERIKSA petugas "
        f"karena hasil pembacaan bisa keliru: {vision_labels}. "
        + f"Masih kosong (mohon dilengkapi petugas): {blank_labels}. "
        + "Catatan: draf ini berkas bantu dari BIMA yang dikirim sebagai lampiran "
        "di chat ini — tidak tersimpan di SIAP. Dokumen final diterbitkan "
        "ber-TTE oleh SIAP saat berkas disetujui."
    )


# ---------------------------------------------------------------------------
# fill_siap_form — write the REAL Formulir Isian on SIAP (text + file slots),
# so the officer only VERIFIES + clicks Simpan and SIAP generates the SK from it.
#
# Unlike draft_sk (a BIMA-authored aid sent into chat, never stored in SIAP),
# this tool upserts SIAP's ptsp.form_value row through the form-value endpoint.
# It REUSES the #80 no-hallucination fill engine
# (siap_templates.resolve_fill_data) for the TEXT fields — profile-sourced +
# Vision-sourced + never-fabricated — and maps the request's APPLICANT-UPLOAD
# docs to the form's FLE file slots by their BIMA doc-type label / filename.
# ---------------------------------------------------------------------------

# The Formulir-Isian TEXT field keys for the vessel/PKPP applicant form. These
# are the SAME key names the SK template uses ([data.KEY]), so the #80 engine's
# classify_placeholder_key already routes them: nama_pemohon/alamat → PROFILE,
# the vessel/permit/letter fields → VISION, jenis_pengadaan → CASE (licence
# name). We pass this exact set to resolve_fill_data so ONLY real reads land in
# the form; anything unread is omitted (never fabricated). Not a per-licence
# hardcode of VALUES — just the field NAMES this applicant form declares.
_FORM_TEXT_FIELD_KEYS: tuple[str, ...] = (
    "jenis_pengadaan",
    "tgl_srt_permohonan",
    "perihal_surat_perm",
    "no_siup",
    "tgl_siup",
    "nama_pemohon",
    "alamat",
    "nama_kapal",
    "thn_bangun",
    "tipe",
    "alat",
    "bahan",
    "gt",
    "galangan",
)

# The 8 FLE file slots on the PKPP applicant form → the alias phrases that
# identify the matching APPLICANT-UPLOAD doc (by its BIMA doc-type label or
# filename). Each doc is classified into AT MOST one slot; an unmatched doc is
# skipped (never forced into a slot). Aliases are matched space/underscore-
# agnostic + case-insensitive against the doc's detected_type, claimed_type, and
# filename (via `_norm_ref`, the same normaliser send_document uses). Ordered
# most-specific-first WITHIN each slot; SIUP is checked before the bare "surat
# permohonan" so "surat izin usaha" never lands in up_permohonan.
_FORM_FILE_SLOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("up_ktp", ("ktp", "kartu tanda penduduk")),
    ("up_pakta_integritas", ("pakta integritas", "pakta")),
    # NB: no bare "oss" alias — as a 3-char token it substring-matched unrelated
    # labels ("Grosse Akta") and mis-slotted the wrong file into the SIUP slot.
    # "siup"/"nib"/"izin usaha" already cover this slot fully.
    ("up_siup_oss", ("siup", "nib", "izin usaha")),
    ("up_gambar_rancang_bangun", (
        "desain kapal", "gambar rancang bangun kapal", "gambar rancang bangun",
        "rancang bangun", "desain",
    )),
    ("up_kontrak", ("surat pesanan", "kontrak", "pesanan")),
    # NB: no bare "api" alias (Alat Penangkap Ikan) — as a 3-char token it
    # substring-matched common Indonesian words (Rekapitulasi, Persiapan, rapi…)
    # and mis-slotted the wrong file into the Spesifikasi slot. The scorer's
    # detected_type label "Spesifikasi Alat Tangkap" carries the real signal.
    ("up_spesifikasi", (
        "spesifikasi alat tangkap", "spesifikasi", "alat tangkap",
    )),
    ("up_srt_kapal", (
        "persetujuan nama kapal", "nama kapal", "surat kapal", "srt kapal",
    )),
    # Surat Permohonan LAST so the more specific SIUP/pesanan/kapal slots claim
    # their docs first — a bare "permohonan" catch-all shouldn't outrank them.
    ("up_permohonan", ("surat permohonan", "permohonan")),
)

# Human labels for the file slots, for the officer-facing note.
_FORM_SLOT_LABELS: dict[str, str] = {
    "up_ktp": "KTP",
    "up_pakta_integritas": "Pakta Integritas",
    "up_siup_oss": "SIUP/NIB",
    "up_permohonan": "Surat Permohonan",
    "up_gambar_rancang_bangun": "Desain Kapal",
    "up_kontrak": "Surat Pesanan",
    "up_spesifikasi": "Spesifikasi Alat Tangkap",
    "up_srt_kapal": "Persetujuan Nama Kapal",
}


def _doc_labels(doc: dict) -> tuple[str, ...]:
    """The normalised labels a doc can be matched on: BIMA's read (detected),
    the citizen's claim, and the filename. Mirrors send_document/_resolve_doc_ref
    so the slot match agrees with what the officer can 'lihat'."""
    return (
        _norm_ref(doc.get("detected_type")),
        _norm_ref(doc.get("claimed_type")),
        _norm_ref(doc.get("filename")),
    )


# The officer doc-context keys the document-API loader emits for an APPLICANT-
# UPLOAD row: `siap:doc:{file_id}` (officer_bridge._load_siap_documents source
# b). ONLY these carry the real ptsp.files.file_id the form-value endpoint can
# copy into a slot (the endpoint requires stereotype=APPLICANT-UPLOAD). The
# other doc-context source — `siap:req:{req_id}` (profile_requirements) — is NOT
# an APPLICANT-UPLOAD file and CANNOT be attached to a slot, so those are skipped
# for the form-file mapping (the officer attaches them by hand if needed).
_SIAP_DOC_FILE_ID = re.compile(r"^siap:doc:(\d+)$")


def _applicant_upload_file_id(ctx_key: Any) -> Optional[int]:
    """Return the real SIAP APPLICANT-UPLOAD file_id for a doc-context key, or
    None. Parses `siap:doc:{id}` (the document-API source, the only one the
    form-value endpoint accepts) and also tolerates a bare integer key (e.g. a
    future loader / a test). A `siap:req:{id}` profile-requirements key returns
    None — it is not an attachable APPLICANT-UPLOAD file."""
    s = str(ctx_key or "")
    m = _SIAP_DOC_FILE_ID.match(s)
    if m:
        return int(m.group(1))
    # Bare integer key (defensive — not the current bridge shape, but harmless).
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _label_tokens(normalized_label: str) -> tuple[str, ...]:
    """Split a normalised label into alphanumeric tokens (drops separators like
    spaces, underscores, dots, dashes). "spesifikasi alat tangkap" → ("spesifikasi",
    "alat", "tangkap"); "siup-oss.pdf" (already norm→"siup oss") → ("siup","oss")."""
    return tuple(t for t in re.split(r"[^a-z0-9]+", normalized_label) if t)


def _alias_matches_label(norm_alias: str, normalized_label: str) -> bool:
    """WHOLE-TOKEN alias match: the alias (tokenised) must appear as a CONTIGUOUS
    token-subsequence of the label (tokenised). So the short alias "nib" matches
    the word "nib" but never a substring inside a longer word ("Grosse", "rapi");
    a multi-word alias ("alat tangkap", "surat pesanan", "gambar rancang bangun")
    still matches as a contiguous run of label tokens. This replaces the previous
    substring containment (`alias in label`) that over-matched short aliases."""
    alias_toks = _label_tokens(norm_alias)
    if not alias_toks:
        return False
    label_toks = _label_tokens(normalized_label)
    n, m = len(label_toks), len(alias_toks)
    if m > n:
        return False
    # Slide the alias token-run over the label tokens; match on a contiguous run.
    for i in range(n - m + 1):
        if label_toks[i:i + m] == alias_toks:
            return True
    return False


def _doc_display_name(doc: dict, ctx_key: Any) -> str:
    """The best human name for a doc in an officer note — filename, else the
    detected/claimed type, else the ctx key."""
    return (
        str(doc.get("filename") or "").strip()
        or str(doc.get("detected_type") or "").strip()
        or str(doc.get("claimed_type") or "").strip()
        or str(ctx_key)
    )


def _map_docs_to_slots(
    documents: dict,
) -> tuple[dict[str, int], list[str], list[str]]:
    """Classify each attachable APPLICANT-UPLOAD doc into ITS form file slot.

    `documents` is the copilot `_doc_context` shape
    ({ctx_key: {filename, detected_type, claimed_type, content, ...}}). For each
    slot (in `_FORM_FILE_SLOTS` order — most-specific slots first) we take the
    FIRST unclaimed doc whose detected/claimed/filename label matches one of the
    slot's alias phrases as a WHOLE-TOKEN run (see `_alias_matches_label` — a
    short alias like "nib" matches the word "nib" but never a substring inside a
    longer word, so "Grosse Akta" no longer mis-slots into the SIUP slot). A doc
    is used for at most one slot; a slot matches at most one doc. Only CONFIDENT
    matches are mapped — an unmatched doc is left out (never forced into a slot).

    The slot value must be the real SIAP `ptsp.files.file_id` (the form-value
    endpoint copies the APPLICANT-UPLOAD by id), NOT the doc-context KEY —
    which is `siap:doc:{file_id}` for the document-API source. We extract that id
    via `_applicant_upload_file_id`; a doc whose key is a profile-requirements
    `siap:req:{id}` (not an APPLICANT-UPLOAD file) yields no id and CANNOT be
    attached through this endpoint — it is recorded in `unattachable` (so the
    officer is told to lampirkan it by hand) rather than silently dropped.

    Returns `(files, mapped_labels, unattachable)`:
      * files        — {slot_name: siap_file_id} for the SIAP form-value call.
      * mapped_labels — human "SlotLabel ← doc-name" lines for the officer note.
      * unattachable  — human "SlotLabel ← doc-name" lines for recognised docs
                        that matched a slot but have no attachable file_id.
    """
    files: dict[str, int] = {}
    mapped_labels: list[str] = []
    unattachable: list[str] = []
    if not documents or not isinstance(documents, dict):
        return files, mapped_labels, unattachable

    used_keys: set = set()
    for slot, aliases in _FORM_FILE_SLOTS:
        norm_aliases = tuple(_norm_ref(a) for a in aliases)
        for ctx_key, doc in documents.items():
            if ctx_key in used_keys:
                continue
            labels = _doc_labels(doc)
            hit = any(
                any(_alias_matches_label(alias, lbl) for lbl in labels if lbl)
                for alias in norm_aliases
            )
            if not hit:
                continue
            # Resolve the REAL SIAP file_id — the endpoint copies the upload by
            # id, so the slot value must be that id, not the ctx key. A doc that
            # isn't an attachable APPLICANT-UPLOAD (profile_requirements source)
            # yields None: it MATCHED but can't be attached, so record it for the
            # officer note and keep scanning this slot for an attachable doc.
            siap_file_id = _applicant_upload_file_id(ctx_key)
            if siap_file_id is None:
                unattachable.append(
                    f"{_FORM_SLOT_LABELS.get(slot, slot)} ← "
                    f"{_doc_display_name(doc, ctx_key)}"
                )
                used_keys.add(ctx_key)  # don't re-consider this doc for other slots
                continue
            files[slot] = siap_file_id
            used_keys.add(ctx_key)
            mapped_labels.append(
                f"{_FORM_SLOT_LABELS.get(slot, slot)} ← "
                f"{_doc_display_name(doc, ctx_key)}"
            )
            break  # this slot is filled; move to the next slot

    return files, mapped_labels, unattachable


async def _perform_form_fill(
    *,
    request_id: int,
    license_id: Optional[int],
    form_id: int,
    profile: Optional[dict],
    case_meta: Optional[dict],
    license_name: Optional[str],
    vision_documents: Optional[dict],
    slot_documents: Optional[list],
) -> dict:
    """SHARED no-hallucination fill core — used by BOTH the officer copilot
    (fill_siap_form) and the citizen auto-fill at submission.

    Given a resolved request/form and the real data handles, it:
      (a) resolves the Formulir-Isian TEXT fields via the #80 engine
          (resolve_fill_data on `_FORM_TEXT_FIELD_KEYS`, run_vision=True) —
          profile-sourced + Vision-sourced + never fabricated;
      (b) maps `slot_documents` to the form's FLE file slots using the SAME
          whole-token alias logic `_map_docs_to_slots` uses (never mis-slots;
          an unmatched doc is skipped);
      (c) upserts the form_value row via siap_form_client.upsert_form.

    Args:
      request_id:     the SIAP license_request id (form-value is keyed by it).
      license_id:     the licence id (context only; the caller already resolved
                      form_id from it).
      form_id:        the discovered APPLICANT Formulir-Isian form_id.
      profile:        person_profile.properties dict (identity fields).
      case_meta:      get_request_case_meta dict (ticket / jenis_pengadaan).
      license_name:   ptsp.license.name (jenis_pengadaan derivation).
      vision_documents: the resolve_fill_data `documents` shape —
          {ctx_key: {"filename","mime_type","content"(bytes),"claimed_type",
                     "detected_type"?}} — for the Vision TEXT pass.
      slot_documents: a normalized list
          [{"file_id": <int SIAP APPLICANT-UPLOAD id>, "filename": <str>,
            "label": <optional bima doc-type label>}] — used ONLY for the file-
          slot mapping. Each entry's file_id is the REAL ptsp.files.file_id the
          form-value endpoint copies into a slot.

    Returns a structured dict (BEST-EFFORT, NEVER raises):
      {
        "ok": bool,                       # SIAP upsert succeeded
        "configured": bool,               # form client configured
        "form_value_id": int|None,
        "filled": {key: provenance_class},# only the keys that got a real value
        "blank_keys": [key, ...],         # fields left blank (officer fills)
        "attached": {slot: file_id},      # files written to slots
        "unattachable": [label, ...],     # matched docs with no attachable id
        "classes": {key: source_class},   # every text field's classification
        "note": str,                      # SIAP message on failure (may be "")
        "http_status": int|None,
      }

    NEVER logs PII (field VALUES) or any SIAP_* token; only counts + ids.
    """
    result: dict[str, Any] = {
        "ok": False,
        "configured": True,
        "form_value_id": None,
        "filled": {},
        "blank_keys": list(_FORM_TEXT_FIELD_KEYS),
        "attached": {},
        "unattachable": [],
        "classes": {},
        "note": "",
        "http_status": None,
    }

    from services import siap_templates as st
    from services.siap_form_client import get_siap_form_client

    # (a) TEXT fields — REUSE the #80 engine so nothing is fabricated.
    vision_documents = vision_documents or {}
    try:
        data, classes = await st.resolve_fill_data(
            list(_FORM_TEXT_FIELD_KEYS),
            profile=profile or {},
            case_meta=case_meta or {},
            license_name=license_name,
            documents=vision_documents,
            run_vision=True,
        )
    except Exception:  # pragma: no cover — resolve_fill_data already guards
        logger.exception(
            "_perform_form_fill: field extraction failed | request_id=%s",
            request_id,
        )
        data, classes = {}, {}

    result["classes"] = dict(classes)
    filled_map = {
        k: classes.get(k, st.SOURCE_BLANK)
        for k in _FORM_TEXT_FIELD_KEYS
        if str(data.get(k) or "").strip()
    }
    result["filled"] = filled_map
    result["blank_keys"] = [
        k for k in _FORM_TEXT_FIELD_KEYS if not str(data.get(k) or "").strip()
    ]

    # (b) FILE slots — map the APPLICANT-UPLOAD docs to slots (confident only),
    #     reusing the SAME whole-token alias logic via a doc-context adapter.
    files, mapped_labels, unattachable = _map_slot_documents(slot_documents)
    result["attached"] = dict(files)
    result["mapped_labels"] = list(mapped_labels)  # "SlotLabel ← doc-name" lines
    result["unattachable"] = list(unattachable)

    # (c) WRITE — best-effort. Any failure returns a structured non-ok result.
    client = get_siap_form_client()
    if not client.is_configured():
        result["configured"] = False
        result["note"] = (
            "Integrasi Formulir Isian SIAP belum dikonfigurasi pada lingkungan "
            "ini."
        )
        return result

    try:
        upsert = await client.upsert_form(
            request_id=int(request_id),
            form_id=int(form_id),
            fields=data,
            files=files,
        )
    except Exception:  # pragma: no cover — client never raises, defence-in-depth
        logger.exception(
            "_perform_form_fill: upsert_form raised | request_id=%s form_id=%s",
            request_id, form_id,
        )
        result["note"] = "Terjadi kendala saat menghubungi SIAP."
        return result

    result["configured"] = bool(upsert.get("configured", True))
    result["http_status"] = upsert.get("http_status")
    result["note"] = str(upsert.get("note") or "")
    result["ok"] = bool(upsert.get("ok"))
    if upsert.get("ok"):
        result["form_value_id"] = upsert.get("form_value_id")

    logger.info(
        "_perform_form_fill | request_id=%s form_id=%s | ok=%s | filled=%d "
        "blank=%d files=%d unattachable=%d | http=%s",
        request_id, form_id, result["ok"], len(filled_map),
        len(result["blank_keys"]), len(files), len(unattachable),
        result["http_status"],
    )
    return result


def _map_slot_documents(
    slot_documents: Optional[list],
) -> tuple[dict[str, int], list[str], list[str]]:
    """Map a NORMALIZED slot-document list to form file slots, reusing the SAME
    whole-token alias logic as `_map_docs_to_slots`.

    `slot_documents` is a list of
      {"file_id": <int SIAP APPLICANT-UPLOAD id>, "filename": <str>,
       "label": <optional bima doc-type label>}
    (the shape the citizen auto-fill and the officer copilot both build). We
    adapt each entry into the `_doc_context` shape `_map_docs_to_slots` expects —
    keying it by `siap:doc:{file_id}` so `_applicant_upload_file_id` recovers the
    REAL SIAP file_id — and delegate to `_map_docs_to_slots` so the alias/slot
    rules stay in ONE place (never mis-slot; skip unmatched). An entry with no
    valid integer file_id is skipped (it can't be attached).

    Returns `(files, mapped_labels, unattachable)` exactly as `_map_docs_to_slots`.
    """
    files: dict[str, int] = {}
    mapped_labels: list[str] = []
    unattachable: list[str] = []
    if not slot_documents:
        return files, mapped_labels, unattachable

    adapted: dict = {}
    for idx, entry in enumerate(slot_documents):
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filename") or "").strip()
        label = str(entry.get("label") or "").strip()
        try:
            fid = int(entry.get("file_id"))
            # An APPLICANT-UPLOAD file → `siap:doc:{id}` so the matcher recovers
            # the REAL SIAP file_id and attaches it.
            ctx_key = f"siap:doc:{fid}"
        except (TypeError, ValueError):
            # No real SIAP file_id (e.g. a profile_requirements doc). Key it as
            # `siap:req:{idx}` so `_map_docs_to_slots` still MATCHES it against a
            # slot alias and SURFACES it as unattachable (never silently dropped,
            # never fabricates a file_id).
            ctx_key = f"siap:req:{idx}"
        # Feed both filename and the bima doc-type label into the matcher (the
        # matcher reads detected_type / claimed_type / filename). The label is
        # the strongest signal, so put it on both type fields.
        adapted[ctx_key] = {
            "filename": filename,
            "detected_type": label,
            "claimed_type": label,
            "content": b"x",  # non-empty marker; bytes are never used here
        }
    return _map_docs_to_slots(adapted)


async def fill_siap_form() -> str:
    """FILL the request's SIAP **Formulir Isian** — its text fields AND its file
    slots — then guide the officer to VERIFY. SIAP generates the SK FROM that
    filled form, so this is the write that actually feeds SIAP's SK generator
    (draft_sk stays the separate BIMA-authored PDF/.docx aid).

    Called when the officer asks to fill/isi the form, prepare the SK, "isikan
    formulirnya", or after a positive review. It:
      1. Resolves the case: request_id + license_id + profile_id (from the SK
         context bound to the session) + the applicant Formulir-Isian form_id
         (siap_db.get_applicant_form_id(license_id) — DISCOVERED, not hardcoded;
         it walks license_approval_step_form → forms filtered to the applicant
         form, excluding the Penomoran form).
      2. Extracts the TEXT field values REUSING the #80 no-hallucination engine
         (siap_templates.resolve_fill_data): profile-sourced (nama_pemohon,
         alamat) from person_profile via profile_id; Vision-sourced (no_siup,
         nama_kapal, gt, …) read off the submitted docs; jenis_pengadaan from
         the licence name. ONLY real reads are included — blanks are omitted,
         never fabricated. Provenance (profile/case vs Vision) is kept so the
         note can flag which values were OCR'd and need eyeballing.
      3. Maps the APPLICANT-UPLOAD docs to the form's FLE file slots by doc-type
         label / filename (KTP→up_ktp, SIUP/NIB→up_siup_oss, …); only confident
         matches are attached.
      4. Calls the form-value endpoint via siap_form_client.upsert_form.
         BEST-EFFORT: on any failure it returns an honest message — never
         crashes the turn.
      5. Returns a provenance-split note: which fields came from official SIAP
         data vs. were read off documents (mohon diperiksa), which files went to
         which slot, which fields it could NOT fill, and a clear instruction to
         verify the Formulir Isian in SIAP and click Simpan (SK is generated by
         SIAP from that form). Includes the officer edit-page link.
    """
    sk = _sk_context.get()
    if not sk or not isinstance(sk, dict):
        return (
            "Formulir Isian belum bisa saya isikan di jalur ini: konteks berkas "
            "(izin & pemohon) tidak tersedia pada sesi. Pada alur chat petugas, "
            "formulir diisi otomatis dari data resmi SIAP."
        )

    license_id = sk.get("license_id")
    ticket = sk.get("ticket")

    if not license_id:
        return (
            "Formulir Isian belum bisa diisi: ID izin (license_id) tidak "
            "diketahui untuk berkas ini, sehingga formulir pemohon di SIAP "
            "tidak dapat ditemukan. Silakan isi Formulir Isian langsung di SIAP."
        )

    try:
        from services import siap_templates as st
        from services import siap_tools as stools
        from services import siap_db as sdb
        from services.siap_form_client import get_siap_form_client
    except Exception:  # pragma: no cover — module import guard
        return (
            "Formulir Isian belum bisa diisi: modul integrasi formulir SIAP "
            "tidak tersedia di server ini. Silakan isi langsung di SIAP."
        )

    # 1) Resolve request_id (needed for BOTH the form-value call and the case
    #    meta) and the applicant form_id (discovered, not hardcoded).
    request_id: Optional[int] = None
    try:
        if ticket:
            request_id = await stools.siap_resolve_request_id(str(ticket))
    except Exception:
        logger.exception("fill_siap_form: request_id resolve failed | ticket bound")
        request_id = None
    if request_id is None:
        return (
            "Formulir Isian belum bisa diisi: nomor permohonan (request_id) "
            "tidak dapat ditemukan di SIAP untuk tiket ini. Silakan isi "
            "Formulir Isian langsung di SIAP."
        )

    form_id: Optional[int] = None
    try:
        form_id = await sdb.get_applicant_form_id(int(license_id))
    except Exception:
        logger.exception(
            "fill_siap_form: form_id resolve failed | license_id=%s", license_id
        )
        form_id = None
    if form_id is None:
        return (
            "Formulir Isian belum bisa diisi: formulir pemohon (Formulir Isian) "
            "untuk izin ini tidak dapat ditemukan di SIAP. Silakan isi langsung "
            "di SIAP."
        )

    # 2) Gather the real data handles the SHARED fill core needs: profile
    #    (identity), case meta + licence name (jenis_pengadaan/ticket), the
    #    in-session docs for the Vision TEXT pass, and the APPLICANT-UPLOAD docs
    #    (as a normalized slot-document list) for the file mapping.
    documents = _doc_context.get() or {}

    # Case meta FIRST so the applicant profile_id can fall back to it: a
    # durable/rehydrated officer session may carry no profile_id (or one encoded
    # before the request was profile-linked), and reading the profile off a
    # stale sk_context alone would leave nama_pemohon/alamat unfilled on the
    # SIAP form even though SIAP has them on the linked profile.
    license_name = sk.get("license_name")
    case_meta: dict = {}
    try:
        case_meta = await sdb.get_request_case_meta(int(request_id))
        if not license_name:
            license_name = await sdb.get_license_name(int(license_id))
    except Exception:
        logger.exception(
            "fill_siap_form: case-meta read failed | license_id=%s", license_id
        )

    profile: dict = {}
    profile_id = sk.get("profile_id") or (case_meta.get("profile_id") if case_meta else None)
    if profile_id is not None:
        try:
            profile = await sdb.get_person_profile_properties(int(profile_id))
        except Exception:
            logger.exception("fill_siap_form: profile read failed | profile_id set")
            profile = {}

    # Build the normalized slot-document list from the officer doc-context. The
    # document-API source keys an APPLICANT-UPLOAD doc `siap:doc:{file_id}`; only
    # those carry the real ptsp.files.file_id the form-value endpoint can copy
    # into a slot. A `siap:req:{id}` (profile_requirements) doc has no attachable
    # file_id — we pass it through with file_id=None + its labels so the shared
    # core still MATCHES it to a slot and SURFACES it as unattachable (never a
    # silent drop). Every entry carries filename + a label for the alias matcher.
    slot_documents: list[dict] = []
    for ctx_key, doc in documents.items():
        if not isinstance(doc, dict):
            continue
        fid = _applicant_upload_file_id(ctx_key)
        label = (
            str(doc.get("detected_type") or "").strip()
            or str(doc.get("claimed_type") or "").strip()
        )
        slot_documents.append({
            "file_id": fid,
            "filename": str(doc.get("filename") or "").strip(),
            "label": label,
        })

    # 3) SHARED no-hallucination fill core — resolves TEXT fields (profile/case/
    #    Vision, never fabricated), maps the file slots, and upserts the form.
    core = await _perform_form_fill(
        request_id=int(request_id),
        license_id=int(license_id),
        form_id=int(form_id),
        profile=profile,
        case_meta=case_meta or ({"ticket": ticket} if ticket else None),
        license_name=license_name,
        vision_documents=documents,
        slot_documents=slot_documents,
    )

    # 4) Render the SAME officer-facing provenance-split note as before, from the
    #    core's structured result: official (profile/case) vs Vision (mohon
    #    diperiksa) vs blank; which files went to which slot; the edit link.
    classes = core.get("classes") or {}
    filled = core.get("filled") or {}
    filled_official = [
        k for k in _FORM_TEXT_FIELD_KEYS
        if k in filled and classes.get(k) in (st.SOURCE_PROFILE, st.SOURCE_CASE)
    ]
    filled_vision = [
        k for k in _FORM_TEXT_FIELD_KEYS
        if k in filled and classes.get(k) == st.SOURCE_VISION
    ]
    blank_fields = list(core.get("blank_keys") or [])
    files = dict(core.get("attached") or {})
    unattachable = list(core.get("unattachable") or [])

    # Use the "SlotLabel ← doc-name" lines the core already computed (so the
    # officer sees WHICH uploaded file went into each slot); fall back to
    # slot-labels only if the core omitted them.
    mapped_labels = list(core.get("mapped_labels") or [
        f"{_FORM_SLOT_LABELS.get(slot, slot)}" for slot in files
    ])
    unattachable_note = (
        " " + "; ".join(unattachable)
        + " dikenali tetapi tidak dapat dilampirkan otomatis — mohon lampirkan "
        "manual di SIAP."
        if unattachable else ""
    )

    official_labels = ", ".join(_pretty_field_label(k) for k in filled_official) or "(tidak ada)"
    vision_labels = ", ".join(_pretty_field_label(k) for k in filled_vision) or "(tidak ada)"
    blank_labels = ", ".join(_pretty_field_label(k) for k in blank_fields) or "(tidak ada)"
    slot_lines = "; ".join(mapped_labels) or "(tidak ada yang cocok otomatis)"

    if not core.get("configured"):
        # No SIAP form seam on this env — tell the officer what BIMA WOULD have
        # filled and to do it by hand, with the edit link. Still useful.
        return (
            "Integrasi pengisian Formulir Isian SIAP belum aktif pada lingkungan "
            "ini, jadi belum bisa saya tuliskan otomatis. Silakan isi Formulir "
            "Isian langsung di SIAP. Sebagai bantuan, nilai yang BIMA baca: "
            f"dari data resmi SIAP — {official_labels}; "
            f"dibaca dari dokumen unggahan (mohon diperiksa) — {vision_labels}. "
            f"Dokumen untuk slot berkas: {slot_lines}.{unattachable_note} "
            + _siap_edit_link_line(request_id)
        )

    if not core.get("ok"):
        # Honest failure — the officer completes the form in SIAP. Still report
        # what BIMA read so the officer can copy it in, and the edit link.
        note = str(core.get("note") or "SIAP menolak permintaan pengisian formulir.")
        return (
            f"Maaf, Formulir Isian belum berhasil saya isikan otomatis. {note} "
            "Silakan isi/lengkapi Formulir Isian langsung di SIAP. Nilai yang "
            f"BIMA baca — dari data resmi SIAP: {official_labels}; dibaca dari "
            f"dokumen unggahan (mohon diperiksa): {vision_labels}. "
            f"Dokumen untuk slot berkas: {slot_lines}.{unattachable_note} "
            + _siap_edit_link_line(request_id)
        )

    # SUCCESS — report what was written, split by provenance, and steer the
    # officer to VERIFY + Simpan. SK is generated by SIAP from this form.
    fields_written = list(filled.keys())
    files_written = list(files.keys())
    return (
        "Formulir Isian sudah saya isikan di SIAP dan siap Anda periksa. "
        f"Terisi dari DATA RESMI SIAP (profil pemohon & data permohonan): "
        f"{official_labels}. "
        f"DIBACA DARI DOKUMEN UNGGAHAN — MOHON DIPERIKSA karena hasil pembacaan "
        f"bisa keliru: {vision_labels}. "
        f"Berkas dilampirkan ke slot: {slot_lines}.{unattachable_note} "
        f"Field yang belum terisi (mohon dilengkapi petugas): {blank_labels}. "
        f"({len(fields_written)} field & {len(files_written)} berkas tertulis.) "
        "Langkah Anda: buka Formulir Isian permohonan ini di SIAP, PERIKSA "
        "setiap isian (terutama yang dibaca dari dokumen), lengkapi yang kosong, "
        "lalu klik Simpan. SK akan dibuat SIAP dari formulir itu. "
        + _siap_edit_link_line(request_id)
    )


# ---------------------------------------------------------------------------
# set_ppkp_number — write the officer-SUPPLIED No. PPKP to SIAP's Penomoran form.
#
# At the Penomoran step the officer ASSIGNS the PPKP licence number (e.g.
# "2026/1234/01"). That number lives on a DIFFERENT form than the applicant
# Formulir Isian — for PKPP it is `no_ppkp` on form 768 ("Form Penomoran PPKP"),
# discovered (never hardcoded) via siap_db.get_form_id_for_field(license_id,
# "no_ppkp"). BIMA writes EXACTLY the value the officer gives — it NEVER
# fabricates a licence number. SIAP then issues the final SK ber-TTE from the
# form. This is the tool the copilot was missing: previously it had no way to
# write an officer-supplied value to a specific field, so it parroted a stale
# "Formulir Isian tidak ditemukan" refusal.
#
# The PPKP number itself is a licence number; to be safe it is kept OUT of logs
# (only its length + the ids are logged). Best-effort: on any failure an honest
# message is returned — the officer turn is NEVER crashed. This is NOT a
# decision/forward action, so it is not confirmation-gated.
# ---------------------------------------------------------------------------


async def set_ppkp_number(nomor_ppkp: str) -> str:
    """WRITE the officer-supplied No. PPKP to the request's SIAP Penomoran form.

    The officer assigns the PPKP licence number at the Penomoran step and asks
    BIMA to fill it (e.g. "isikan nomor ppkp 2026/1234/01"). This tool writes
    EXACTLY that value to the `no_ppkp` field of the licence's Penomoran form —
    the form is DISCOVERED via siap_db.get_form_id_for_field(license_id,
    "no_ppkp"), not hardcoded. BIMA never fabricates the number: if the officer
    supplies nothing, it asks for it rather than writing a guess.

    Best-effort — degrades to an honest message on any missing context / unresolved
    request / undiscoverable form / SIAP failure. NEVER raises. Returns a
    confirmation that the number was written to the SIAP Penomoran form and the
    officer should VERIFY + Simpan; SIAP issues the final SK ber-TTE from the form.
    Includes the officer edit-page link. Does NOT auto-forward/decide.
    """
    # (a) Need the SK context (license_id + ticket) bound to the session.
    sk = _sk_context.get()
    if not sk or not isinstance(sk, dict):
        return (
            "No. PPKP belum bisa saya isikan di jalur ini: konteks berkas (izin & "
            "permohonan) tidak tersedia pada sesi. Pada alur chat petugas, No. PPKP "
            "ditulis ke form Penomoran SIAP dari konteks berkas yang aktif."
        )

    # (b) Trim the officer-supplied number. Empty → ask, do NOT write anything.
    number = str(nomor_ppkp or "").strip()
    if not number:
        return (
            "Silakan sebutkan nomor PPKP-nya (mis. \"2026/1234/01\"), lalu saya "
            "tuliskan ke form Penomoran di SIAP. Saya hanya menuliskan nomor yang "
            "Anda berikan — tidak mengarang."
        )

    license_id = sk.get("license_id")
    ticket = sk.get("ticket")
    if not license_id:
        return (
            "No. PPKP belum bisa ditulis: ID izin (license_id) tidak diketahui "
            "untuk berkas ini, sehingga form Penomoran di SIAP tidak dapat "
            "ditemukan. Silakan isikan No. PPKP langsung di SIAP."
        )

    try:
        from services import siap_tools as stools
        from services import siap_db as sdb
        from services.siap_form_client import get_siap_form_client
    except Exception:  # pragma: no cover — module import guard
        return (
            "No. PPKP belum bisa ditulis: modul integrasi formulir SIAP tidak "
            "tersedia di server ini. Silakan isikan langsung di SIAP."
        )

    # (c) Resolve the request_id from the ticket (needed for the form-value write).
    request_id: Optional[int] = None
    try:
        if ticket:
            request_id = await stools.siap_resolve_request_id(str(ticket))
    except Exception:
        logger.exception("set_ppkp_number: request_id resolve failed | ticket bound")
        request_id = None
    if request_id is None:
        return (
            "No. PPKP belum bisa ditulis: nomor permohonan (request_id) tidak dapat "
            "ditemukan di SIAP untuk tiket ini. Silakan isikan No. PPKP langsung di "
            "SIAP."
        )

    # (d) DISCOVER the form that owns `no_ppkp` for this licence (the Penomoran
    #     form — 768 for PKPP, by discovery, never hardcoded).
    form_id: Optional[int] = None
    try:
        form_id = await sdb.get_form_id_for_field(int(license_id), "no_ppkp")
    except Exception:
        logger.exception(
            "set_ppkp_number: form_id resolve failed | license_id=%s", license_id
        )
        form_id = None
    if form_id is None:
        return (
            "No. PPKP belum bisa ditulis: form Penomoran (field no_ppkp) untuk izin "
            "ini tidak dapat ditemukan di SIAP. Silakan isikan No. PPKP langsung di "
            "SIAP."
        )

    # (e) WRITE exactly the officer-supplied value to the discovered form. The
    #     PPKP number is a licence number — kept OUT of logs; only its LENGTH +
    #     the ids are logged.
    client = get_siap_form_client()
    if not client.is_configured():
        logger.info(
            "set_ppkp_number: form client not configured | license_id=%s "
            "request_id=%s form_id=%s len=%d",
            license_id, request_id, form_id, len(number),
        )
        return (
            "Integrasi penulisan form Penomoran SIAP belum aktif pada lingkungan "
            "ini, jadi No. PPKP belum bisa saya tuliskan otomatis. Silakan isikan "
            "No. PPKP langsung di form Penomoran di SIAP. "
            + _siap_edit_link_line(request_id)
        )

    # (f) BEST-EFFORT — any failure returns an honest message, NEVER raises.
    try:
        upsert = await client.upsert_form(
            request_id=int(request_id),
            form_id=int(form_id),
            fields={"no_ppkp": number},
            files={},
        )
    except Exception:  # pragma: no cover — client never raises, defence-in-depth
        logger.exception(
            "set_ppkp_number: upsert_form raised | license_id=%s request_id=%s "
            "form_id=%s len=%d",
            license_id, request_id, form_id, len(number),
        )
        return (
            "Maaf, terjadi kendala saat menghubungi SIAP, jadi No. PPKP belum "
            "tertulis. Silakan isikan No. PPKP langsung di form Penomoran di SIAP. "
            + _siap_edit_link_line(request_id)
        )

    ok = bool(isinstance(upsert, dict) and upsert.get("ok"))
    logger.info(
        "set_ppkp_number | license_id=%s request_id=%s form_id=%s len=%d ok=%s",
        license_id, request_id, form_id, len(number), ok,
    )
    if not ok:
        note = str((upsert or {}).get("note") or "") if isinstance(upsert, dict) else ""
        note = (" " + note) if note else ""
        return (
            "Maaf, No. PPKP belum berhasil saya tuliskan otomatis ke SIAP." + note
            + " Silakan isikan No. PPKP langsung di form Penomoran di SIAP. "
            + _siap_edit_link_line(request_id)
        )

    # (g) SUCCESS — confirm, steer the officer to verify + Simpan, note that SIAP
    #     issues the final SK ber-TTE from the form. Do NOT auto-forward/decide.
    return (
        f"No. PPKP sudah saya tuliskan ke form Penomoran PPKP di SIAP: {number}. "
        "Langkah Anda: buka form Penomoran permohonan ini di SIAP, PERIKSA nomornya, "
        "lalu klik Simpan. SK final ber-TTE diterbitkan SIAP dari form itu — saya "
        "tidak meneruskan atau memutuskan berkas ini. "
        + _siap_edit_link_line(request_id)
    )


def _siap_edit_link_line(request_id: Optional[int]) -> str:
    """One-line officer edit-page link for the request, or a blank string.

    Reuses SIAP_SIGNING_URL_BASE (the SIAP Filament admin base) with the
    daftar-permohonans edit route. Best-effort — a missing request_id yields no
    link line rather than a broken URL."""
    if request_id is None:
        return ""
    url = f"{_SIAP_SIGNING_BASE}/admin/daftar-permohonans/{int(request_id)}/edit"
    return f"Buka Formulir Isian di SIAP: {url}"


def compare_field(doc_a: dict, doc_b: dict, field: str) -> dict:
    """
    Pure compare of one field across two extracted-document dicts.

    Uses validator-aligned name normalisation when the field is name-like
    (heuristic: field contains 'nama' or 'name') so two KTPs of the same
    person with different honorifics still compare equal.
    """
    raw_a = doc_a.get(field, "") if isinstance(doc_a, dict) else ""
    raw_b = doc_b.get(field, "") if isinstance(doc_b, dict) else ""

    is_name_like = bool(re.search(r"nama|name", field, re.IGNORECASE))

    if is_name_like:
        norm_a, norm_b = _normalize_name(raw_a), _normalize_name(raw_b)
    else:
        norm_a = re.sub(r"\s+", " ", str(raw_a or "")).strip().upper()
        norm_b = re.sub(r"\s+", " ", str(raw_b or "")).strip().upper()

    if not norm_a or not norm_b:
        return {
            "equal": False,
            "similarity": 0.0,
            "note": (
                "Salah satu nilai kosong — tidak bisa dibandingkan. "
                f"doc_a.{field}={raw_a!r}, doc_b.{field}={raw_b!r}."
            ),
        }

    similarity = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
    equal = norm_a == norm_b

    if equal:
        note = f"Nilai identik setelah normalisasi: {norm_a!r}."
    elif similarity >= 0.85:
        note = (
            f"Mirip ({int(similarity * 100)}%) tapi tidak identik. "
            f"doc_a={raw_a!r}, doc_b={raw_b!r}."
        )
    else:
        note = (
            f"Berbeda ({int(similarity * 100)}% kemiripan). "
            f"doc_a={raw_a!r}, doc_b={raw_b!r}."
        )

    return {
        "equal": equal,
        "similarity": round(similarity, 4),
        "note": note,
    }


# ---------------------------------------------------------------------------
# Cross-document identity comparison — the headline verificator feature
# ([[Decisions]] §22). The officer asks "bandingkan NIK di KTP dengan surat
# permohonan" / "cocokkan nama di KTP dan surat permohonan". `compare_field`
# is a PURE comparator that needs two already-extracted field dicts, but the
# copilot has no tool that READS the per-doc identity fields out of the raw
# uploads. So the model would call get_doc_summary (prose only) and dead-end.
#
# This tool closes that gap: it resolves two doc refs against the in-session
# bytes, runs one lightweight Gemini Vision identity-extraction per doc (NIK +
# nama), and compares with the SAME validator-aligned normalisation
# `compare_field` uses. Degrades cleanly when a field can't be read or a doc's
# bytes are gone. NIK is masked in the officer-facing result (the officer-brief
# policy) and NEVER logged.
# ---------------------------------------------------------------------------

_IDENTITY_EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nik": {
            "type": "string",
            "description": (
                "The 16-digit NIK (Nomor Induk Kependudukan) printed on the "
                "document, digits only. Empty string if none is visible."
            ),
        },
        "nama": {
            "type": "string",
            "description": (
                "The person's full name as printed on the document (the "
                "applicant / pemohon / cardholder). Empty string if none is "
                "visible."
            ),
        },
    },
    "required": ["nik", "nama"],
}

_IDENTITY_EXTRACT_PROMPT = (
    "Anda asisten petugas perizinan. Dari dokumen terlampir, ambil identitas "
    "pemohon: 'nik' (Nomor Induk Kependudukan, 16 digit, angka saja) dan "
    "'nama' (nama lengkap orang yang tercantum). Jika sebuah field tidak "
    "terlihat, kembalikan string kosong. Jangan mengarang — hanya yang benar-"
    "benar terbaca di dokumen."
)

# Which identity field the officer's free-text `field` refers to.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "nik": ("nik", "nomor induk", "no ktp", "nomor ktp", "no. ktp"),
    "nama": ("nama", "name", "pemilik", "pemohon", "atas nama"),
}


def _canonical_identity_field(field: str) -> str:
    """Map the officer's free-text field ('NIK', 'nama pemohon') onto one of
    the identity keys we extract ('nik' | 'nama'). Defaults to 'nik' — the most
    common verificator ask — when nothing matches."""
    f = _norm_ref(field)
    for key, aliases in _FIELD_ALIASES.items():
        if any(a in f for a in aliases):
            return key
    return "nik"


async def _extract_identity(doc: dict) -> Optional[dict[str, str]]:
    """Run one Gemini Vision identity extraction over an in-session doc's bytes.
    Returns {'nik','nama'} (values may be '') or None when Vision is
    unconfigured / fails / the bytes are absent. Bytes are never logged."""
    content = doc.get("content") if isinstance(doc, dict) else None
    if not content:
        return None
    from services.gemini_vision import extract_structured, is_configured

    if not is_configured():
        return None
    parsed = await extract_structured(
        image_bytes=content,
        mime_type=doc.get("mime_type", "application/octet-stream"),
        prompt=_IDENTITY_EXTRACT_PROMPT,
        response_schema=_IDENTITY_EXTRACT_SCHEMA,
    )
    if not parsed or not isinstance(parsed, dict):
        return None
    return {
        "nik": re.sub(r"\D", "", str(parsed.get("nik") or "")),
        "nama": str(parsed.get("nama") or "").strip(),
    }


async def compare_identity(doc_a: str, doc_b: str, field: str = "nik") -> dict:
    """
    Compare an IDENTITY field (NIK or name) between two uploaded documents —
    the headline verificator ask, e.g. "bandingkan NIK di KTP dengan surat
    permohonan" or "cocokkan nama di KTP dan surat permohonan".

    `doc_a`/`doc_b` are document REFS the officer speaks (a type/label like
    "KTP" / "surat permohonan", or a file_id). `field` is the field to compare
    ('nik' or 'nama'; free text like "NIK"/"nama pemohon" is accepted).

    The tool resolves both refs against the in-session document bytes, extracts
    the requested field from each via Gemini Vision, and compares with the same
    normalisation the validator uses (honorific-tolerant for names, digits-only
    for NIK). Returns a structured, officer-narratable result — never raises.

    On the admin-dashboard path (no in-session bytes) it says so plainly so the
    model can direct the officer to the case page's validation instead of
    dead-ending. NIK is MASKED in the returned strings (officer-brief policy);
    the raw digits are compared internally but never surfaced or logged.
    """
    ctx = _doc_context.get()
    key = _canonical_identity_field(field)
    if not ctx or not isinstance(ctx, dict):
        return {
            "available": False,
            "note": (
                "Perbandingan lintas-dokumen butuh berkas asli yang dikirim di "
                "sesi ini. Pada halaman kasus, gunakan hasil validasi (temuan "
                "konsistensi) untuk kecocokan NIK/nama antar dokumen."
            ),
        }

    fid_a = _resolve_doc_ref(ctx, doc_a)
    fid_b = _resolve_doc_ref(ctx, doc_b)
    missing = [
        ref for ref, fid in ((doc_a, fid_a), (doc_b, fid_b)) if fid is None
    ]
    if missing:
        labels = sorted(
            (d.get("filename") or d.get("detected_type")
             or d.get("claimed_type") or fid)
            for fid, d in ctx.items()
        )
        return {
            "available": False,
            "note": (
                f"Dokumen tidak ditemukan di sesi ini: {', '.join(missing)}. "
                f"Dokumen tersedia: {', '.join(labels) or '(tidak ada)'}."
            ),
        }

    id_a = await _extract_identity(ctx.get(fid_a) or {})
    id_b = await _extract_identity(ctx.get(fid_b) or {})
    label_a = (ctx.get(fid_a) or {}).get("filename") or doc_a
    label_b = (ctx.get(fid_b) or {}).get("filename") or doc_b

    if id_a is None or id_b is None:
        return {
            "available": False,
            "field": key,
            "note": (
                f"Tidak dapat membaca {key.upper()} dari salah satu dokumen "
                f"({label_a} / {label_b}) — berkas mungkin tidak lagi tersimpan "
                "atau tidak terbaca jelas."
            ),
        }

    val_a = id_a.get(key, "")
    val_b = id_b.get(key, "")

    # Reuse the pure comparator so normalisation matches the validator exactly.
    cmp = compare_field({key: val_a}, {key: val_b}, key)

    # Officer-facing display: mask NIK (brief policy); names shown in the clear
    # to the authorized officer, matching how the validator's name-consistency
    # finding already surfaces both names. Nothing here is logged.
    def _display(v: str) -> str:
        if not v:
            return "(tidak terbaca)"
        return mask_nik(v) if key == "nik" else v

    return {
        "available": True,
        "field": key,
        "doc_a": label_a,
        "doc_b": label_b,
        "value_a": _display(val_a),
        "value_b": _display(val_b),
        "equal": cmp["equal"],
        "similarity": cmp["similarity"],
        "note": (
            f"{key.upper()} pada {label_a}: {_display(val_a)}; pada {label_b}: "
            f"{_display(val_b)}. "
            + (
                "KEDUANYA COCOK." if cmp["equal"]
                else f"TIDAK identik (kemiripan {int(cmp['similarity'] * 100)}%)."
            )
        ),
    }


def cite_regulation(query: str) -> list[dict]:
    """
    Search ChromaDB for the top 3 most relevant OSS-RBA chunks. Returns a
    list of `{title, snippet, citation}` dicts that the model can quote
    verbatim. Empty list when ChromaDB is empty or unreachable — the model
    is told to handle that by saying it cannot find a citation.
    """
    chunks = query_regulations(query, n_results=3)
    out: list[dict] = []
    for chunk in chunks:
        kbli = chunk.get("kbli_code", "") or ""
        section = chunk.get("section", "") or "umum"
        skala = chunk.get("skala", "") or ""
        source_url = chunk.get("source_url", "") or ""

        title_bits = [f"KBLI {kbli}" if kbli else "OSS-RBA"]
        if section:
            title_bits.append(section)
        if skala:
            title_bits.append(skala)
        title = " — ".join(title_bits)

        content = chunk.get("content", "") or ""
        snippet = content[:200] + ("…" if len(content) > 200 else "")

        citation = source_url or (f"KBLI {kbli}" if kbli else "OSS-RBA")

        out.append({
            "title": title,
            "snippet": snippet,
            "citation": citation,
        })
    return out


# ---------------------------------------------------------------------------
# Chained-context read tool — Vision req #11.
#
# When an officer opens a case, the next desk must see what the previous desk
# said. SIAP's forward/decision endpoints append their notes to
# `ptsp.license_log`; `siap_get_status_timeline` already reads that log
# (per-step `status` + `description` + owning desk). This tool reuses it and
# reshapes the result into a clean "prior-stage notes" view the model can
# narrate. We do NOT re-implement the SQL — siap_tools owns that.
# ---------------------------------------------------------------------------

# license_log statuses that carry an officer-written note worth surfacing.
_NOTE_BEARING_STATUSES = {"APPROVED", "REJECTED", "SUBMITTED", "VERIFIKASI", "DITOLAK"}


async def get_case_log_notes(ticket: str) -> dict:
    """
    Return the chain of notes prior desks wrote into SIAP's `license_log`
    for this case, so the officer now holding the file sees the handover
    context (Vision req #11).

    Built on `siap_get_status_timeline` — every forward/decision note lands
    in `license_log`, which that tool already reads. We reshape its step
    list into `{step, status, owner_desk, note, entered_on}` entries,
    oldest-first, dropping steps with no note text.

    Returns:
      {
        "found": bool,
        "ticket": str,
        "request_id": int | None,   # also the id the write tools need
        "note_count": int,
        "notes": [ {step, status, owner_desk, note, entered_on}, ... ],
      }
    On miss / SIAP unavailable: found=False with a `note` field. Never raises.
    """
    timeline = await siap_get_status_timeline(ticket)
    if not timeline.get("found"):
        return {
            "found": False,
            "ticket": ticket,
            "note": timeline.get(
                "note", "Riwayat catatan untuk tiket ini tidak tersedia."
            ),
        }

    notes: list[dict[str, Any]] = []
    for step in timeline.get("steps") or []:
        if not isinstance(step, dict):
            continue
        desc = str(step.get("step", "") or "").strip()
        status = str(step.get("status", "") or "").strip().upper()
        # The step `description` from license_log is the officer's note.
        # Skip purely structural rows with no human text.
        if not desc or desc.lower() in {"tahapan", "-"}:
            continue
        notes.append({
            "step": desc,
            "status": status,
            "owner_desk": step.get("owner_desk"),
            "note": desc,
            "entered_on": step.get("entered_on"),
        })

    return {
        "found": True,
        "ticket": timeline.get("no_tiket", ticket),
        "request_id": timeline.get("request_id"),
        "note_count": len(notes),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Signature handoff tool — Wave 4, Vision req #13.
#
# BIMA does NOT sign. SIAP Jateng owns the digital signature (TTE/BSRE) via
# its "Tanda Tangan Berkas" Filament resource. This tool builds the deep-link
# that opens SIAP's signing surface for a specific case so the Head of
# DPMPTSP can review with BIMA, then click through and sign in SIAP.
#
# SIAP route (verified against the SIAP repo, 2026-05-21):
#   * Filament panel:   AdminPanelProvider → ->id('admin') ->path('admin')
#   * Resource:         App\Filament\Resources\TandaTanganBerkasResource
#                       protected static ?string $slug = 'tanda-tangan-berkas';
#                       single page ManageTandaTanganBerkas at route '/'.
#   * The TTE action is a Filament table EditAction named 'tte' on that page;
#     the table column `properties.ticket` is `searchable`.
# So the signing page is `<base>/admin/tanda-tangan-berkas`, and we pre-filter
# it to the one case by appending Filament's table-search query param
# (`?tableSearch=<ticket>`) — the ticket is the stable, public-knowledge
# handle (BIMA does not own SIAP's internal `request_id` record key).
# ---------------------------------------------------------------------------

# Filament's table-search query parameter. Filament v3 hydrates the table
# search box from `?tableSearch=`, so the Head lands on the signing list
# already filtered to this one ticket.
_SIAP_SIGNING_PATH = "/admin/tanda-tangan-berkas"


def get_siap_signing_link(ticket: str) -> dict:
    """
    Build the deep-link that opens SIAP Jateng's signing page for a case.

    BIMA never signs — SIAP owns TTE/BSRE. This tool is the HANDOFF: it
    returns the URL of SIAP's "Tanda Tangan Berkas" Filament page,
    pre-filtered to `ticket` via Filament's `tableSearch` query param, so
    the Head of DPMPTSP clicks through and signs in SIAP itself.

    Args:
      ticket: the SIAP ticket of the case to sign.

    Returns:
      {
        "available": bool,
        "ticket": str,
        "url": str,            # the SIAP signing deep-link
        "label": str,          # the call-to-action text, Indonesian
        "note": str,           # one-line guidance for the model to narrate
      }
    On a missing ticket returns `{"available": False, "note": "..."}`.
    """
    ticket = (ticket or "").strip()
    if not ticket:
        return {
            "available": False,
            "note": (
                "Nomor tiket tidak tersedia — tautan tanda tangan SIAP "
                "tidak dapat dibuat."
            ),
        }

    # Zero-padded 9-digit ticket matches what SIAP stores in
    # `license_request.properties->>'ticket'`, so the table search hits.
    padded = ticket.zfill(9) if ticket.isdigit() else ticket
    url = f"{_SIAP_SIGNING_BASE}{_SIAP_SIGNING_PATH}?tableSearch={quote(padded)}"
    return {
        "available": True,
        "ticket": padded,
        "url": url,
        "label": "Tanda tangani di SIAP Jateng",
        "note": (
            "Tautan ini membuka halaman Tanda Tangan Berkas di SIAP Jateng "
            f"yang sudah tersaring ke tiket {padded}. Penandatanganan "
            "(TTE/BSRE) dilakukan di SIAP menggunakan passphrase BSrE "
            "milik penandatangan — BIMA tidak menandatangani dokumen."
        ),
    }


# ---------------------------------------------------------------------------
# Write tools — Wave 2. OFFICER-IN-THE-LOOP.
#
# `forward_case` and `record_decision` are the only side-effecting tools in
# the copilot. They reach SIAP through `services.siap_write_client`. The hard
# guard against an unconfirmed write is a `confirmed: bool` argument that is
# checked HERE, in Python — it does not rely on the model behaving. When
# `confirmed` is false (or absent) the tool returns a DRAFT and performs no
# write at all. The model is told (system prompt) that `confirmed=true` is
# only legal right after an explicit officer "ya"/"setuju". So a write needs
# BOTH: the model must pass confirmed=true AND, even if it wrongly does so,
# the draft path is the default — there is no code path that writes without
# `confirmed is True`.
#
# The write endpoints address a request by numeric `request_id`, not by
# ticket. We resolve it from the ticket via `get_case_log_notes` (which wraps
# siap_get_status_timeline) so a caller only ever needs the ticket.
# ---------------------------------------------------------------------------

# Tool names that MUST receive an explicit confirmation. Referenced by the
# chat loop for defence-in-depth logging.
_REQUIRES_CONFIRMATION = {"forward_case", "record_decision"}


async def _resolve_request_id(ticket: str) -> tuple[Optional[int], Optional[str]]:
    """Resolve a SIAP `request_id` from a ticket. Returns (request_id, error).

    On success error is None; on failure request_id is None and error holds
    an Indonesian explanation the write tool can return to the model.
    """
    log = await get_case_log_notes(ticket)
    if not log.get("found"):
        return None, log.get(
            "note", f"Tidak dapat menemukan permohonan untuk tiket {ticket}."
        )
    request_id = log.get("request_id")
    if request_id is None:
        return None, (
            f"Permohonan untuk tiket {ticket} ditemukan tetapi request_id "
            "tidak tersedia — tindakan tulis tidak bisa dijalankan."
        )
    return int(request_id), None


async def forward_case(
    ticket: str,
    note: str = "",
    confirmed: bool = False,
) -> dict:
    """
    Forward a case to the NEXT approval desk in SIAP.

    OFFICER-IN-THE-LOOP: this tool only performs the write when
    `confirmed is True`. With `confirmed` false/absent it returns a DRAFT
    (`{"executed": False, "draft": {...}}`) describing exactly what would
    happen — the copilot must show that draft and get an explicit officer
    "ya" before calling again with `confirmed=true`.

    Args:
      ticket: the 9-digit SIAP ticket of the case to forward.
      note: optional officer note (max 1000 chars) recorded into
        `license_log` so the next desk sees why the file moved.
      confirmed: MUST be true to actually forward. The system prompt makes
        this legal only directly after an explicit officer confirmation.

    Returns a structured dict — never raises:
      draft  → {"executed": False, "draft": {...}, "needs_confirmation": True}
      done   → {"executed": True, "ok": True, "result": {...}}
      failed → {"executed": True, "ok": False, "note": "..."}
    """
    ticket = (ticket or "").strip()
    note = (note or "").strip()

    if not confirmed:
        # DRAFT path — no write. This is the default and the safe path.
        return {
            "executed": False,
            "needs_confirmation": True,
            "draft": {
                "action": "forward_case",
                "ticket": ticket,
                "note": note,
                "description": (
                    f"Akan meneruskan berkas tiket {ticket} ke meja "
                    f"persetujuan berikutnya di SIAP"
                    + (f", dengan catatan: \"{note}\"." if note else ".")
                ),
            },
            "instruction_to_model": (
                "Tampilkan draf ini kepada petugas dan minta konfirmasi "
                "eksplisit ('ya'). JANGAN memanggil forward_case dengan "
                "confirmed=true sebelum petugas menjawab ya."
            ),
        }

    write_client = get_siap_write_client()
    if not write_client.is_configured():
        return {
            "executed": False,
            "ok": False,
            "note": (
                "Integrasi tulis SIAP belum dikonfigurasi (SIAP_WRITE_API_TOKEN "
                "kosong). Berkas tidak diteruskan."
            ),
        }

    request_id, error = await _resolve_request_id(ticket)
    if error is not None:
        return {"executed": True, "ok": False, "note": error}

    logger.info(
        "Copilot WRITE forward_case | ticket=%s | request_id=%s | confirmed=True",
        ticket, request_id,
    )
    result = await write_client.forward_request(request_id, note=note or None)
    return {
        "executed": True,
        "ok": bool(result.get("ok")),
        "result": result.get("data") if result.get("ok") else None,
        "note": result.get("note", ""),
    }


async def record_decision(
    ticket: str,
    decision: str,
    notes: str,
    confirmed: bool = False,
) -> dict:
    """
    Record an officer's APPROVE or REJECT decision on a case in SIAP.

    OFFICER-IN-THE-LOOP: this is the most accountable action the copilot
    can take, so the guard is the same as `forward_case` — the write only
    happens when `confirmed is True`. With `confirmed` false/absent it
    returns a DRAFT and writes NOTHING. A `rejected` decision routes the
    case back to the previous desk on the SIAP side.

    Args:
      ticket: the 9-digit SIAP ticket of the case.
      decision: "approved" (terima) or "rejected" (tolak).
      notes: the officer's reasoning — REQUIRED, max 1000 chars. This is
        written into `license_log` so the next/previous desk sees it.
      confirmed: MUST be true to actually record the decision. Legal only
        directly after an explicit officer confirmation (system prompt).

    Returns a structured dict — never raises:
      draft  → {"executed": False, "draft": {...}, "needs_confirmation": True}
      done   → {"executed": True, "ok": True, "result": {...}}
      failed → {"executed": True, "ok": False, "note": "..."}
    """
    ticket = (ticket or "").strip()
    decision_norm = (decision or "").strip().lower()
    notes = (notes or "").strip()

    decision_label = {"approved": "MENERIMA", "rejected": "MENOLAK"}.get(
        decision_norm, decision_norm.upper() or "(tidak ditentukan)"
    )

    if not confirmed:
        # DRAFT path — no write. Safe default.
        return {
            "executed": False,
            "needs_confirmation": True,
            "draft": {
                "action": "record_decision",
                "ticket": ticket,
                "decision": decision_norm,
                "notes": notes,
                "description": (
                    f"Akan mencatat keputusan {decision_label} izin untuk "
                    f"tiket {ticket} di SIAP"
                    + (f", dengan alasan: \"{notes}\"." if notes else
                       " (PERINGATAN: alasan keputusan masih kosong).")
                ),
            },
            "instruction_to_model": (
                "Tampilkan draf keputusan ini kepada petugas dan minta "
                "konfirmasi eksplisit ('ya'). Keputusan izin bersifat "
                "final dan menjadi tanggung jawab petugas — JANGAN "
                "memanggil record_decision dengan confirmed=true sebelum "
                "petugas menjawab ya."
            ),
        }

    write_client = get_siap_write_client()
    if not write_client.is_configured():
        return {
            "executed": False,
            "ok": False,
            "note": (
                "Integrasi tulis SIAP belum dikonfigurasi (SIAP_WRITE_API_TOKEN "
                "kosong). Keputusan tidak dicatat."
            ),
        }

    request_id, error = await _resolve_request_id(ticket)
    if error is not None:
        return {"executed": True, "ok": False, "note": error}

    logger.info(
        "Copilot WRITE record_decision | ticket=%s | request_id=%s | "
        "decision=%s | confirmed=True",
        ticket, request_id, decision_norm,
    )
    result = await write_client.record_decision(
        request_id, decision=decision_norm, notes=notes
    )
    return {
        "executed": True,
        "ok": bool(result.get("ok")),
        "result": result.get("data") if result.get("ok") else None,
        "note": result.get("note", ""),
    }


# ---------------------------------------------------------------------------
# Function declarations for Gemini.
#
# Gemini's `functionDeclarations` accepts the OpenAPI 3.0 subset (same family
# as gemini_vision.py's responseSchema). Only the types string / integer /
# number / boolean / array / object are allowed; no `$ref`, no `oneOf`,
# no `additionalProperties`. Spec: https://ai.google.dev/api/generate-content
# (Tool & FunctionDeclaration sections).
# ---------------------------------------------------------------------------

_FUNCTION_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_validation_summary",
        "description": (
            "Ambil hasil validasi BIMA untuk tiket yang sedang dianalisis: "
            "skor kelengkapan, status, dan daftar masalah yang sudah "
            "diurutkan dari yang paling parah. WAJIB dipanggil di awal sesi "
            "atau saat petugas bertanya tentang kondisi/kelayakan berkas. "
            "Tidak butuh argumen — konteksnya sudah terikat ke sesi."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_case_full",
        "description": (
            "Ambil seluruh data permohonan izin dari SIAP berdasarkan nomor "
            "tiket (9 digit, zero-padded). Gunakan ini di awal sesi untuk "
            "mendapatkan konteks faktual kasus."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit (mis. '000077591').",
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "get_doc_summary",
        "description": (
            "Ringkas isi satu dokumen pendukung yang diunggah (KTP, surat "
            "permohonan, proposal, pakta integritas, spesifikasi alat "
            "tangkap, dll). Gunakan saat petugas minta ringkasan/isi dokumen, "
            "mis. 'apa isi proposalnya', 'ringkas KTP-nya'. Argumen `doc_ref` "
            "adalah JENIS/NAMA dokumen (mis. 'KTP', 'surat permohonan', "
            "'proposal') ATAU file_id — sebutkan sesuai kata petugas; tidak "
            "perlu tahu id file. Pada alur chat, ringkasan dibuat dengan "
            "Gemini Vision atas berkas asli; bila berkas sudah tidak tersimpan, "
            "tool tetap memberi ringkasan dari catatan pemeriksaan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_ref": {
                    "type": "string",
                    "description": (
                        "Jenis/nama dokumen yang diminta (mis. 'KTP', 'surat "
                        "permohonan', 'proposal') atau file_id."
                    ),
                },
            },
            "required": ["doc_ref"],
        },
    },
    {
        "name": "send_document",
        "description": (
            "Kirimkan berkas dokumen ASLI yang diunggah ke chat petugas, "
            "supaya petugas dapat MELIHAT/MEMBUKA/menerima dokumennya sendiri. "
            "WAJIB dipanggil setiap kali petugas ingin melihat/lihat/liat/buka/"
            "tampilkan/kirim sebuah dokumen — baik permintaan UMUM ('lihat "
            "dokumen yang diupload', 'kirim berkas yang dikirim') MAUPUN dengan "
            "MENYEBUT NAMA ('saya mau liat surat permohonannya', 'kirim KTP-nya', "
            "'boleh saya lihat surat pesanannya', 'buka desain kapalnya', 'tolong "
            "kirimkan proposalnya'). Argumen `doc_ref` adalah label/jenis dokumen "
            "(mis. 'KTP', 'Surat Permohonan', 'Surat Pesanan', 'proposal', "
            "'desain kapal') atau file_id; untuk permintaan umum, sebut dokumen "
            "yang tersedia. Berbeda dari get_doc_summary yang hanya MERINGKAS — "
            "tool ini benar-benar MENGIRIM berkasnya. JANGAN menjawab dengan "
            "prosa saja saat petugas minta melihat dokumen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_ref": {
                    "type": "string",
                    "description": (
                        "Jenis/label dokumen yang diminta (mis. 'KTP', "
                        "'Surat Pesanan', 'NIB') atau file_id dokumen."
                    ),
                },
            },
            "required": ["doc_ref"],
        },
    },
    {
        "name": "draft_sk",
        "description": (
            "Buatkan DRAF dokumen output resmi (Rekomendasi Teknis ATAU Surat "
            "Keputusan/SK) yang TEPAT untuk meja saat ini, dari template RESMI "
            "SIAP, terisi otomatis dari DATA RESMI (profil pemohon SIAP, data "
            "permohonan, dokumen unggahan), lalu kirim ke petugas sebagai PDF "
            "(untuk dibaca) + .docx (untuk diedit). Berlaku untuk SEMUA jenis "
            "izin, bukan hanya PPKP — tool memilih sendiri dokumen & mengisi "
            "field sesuai template izin itu. Gunakan saat petugas meminta "
            "'draftkan SK', 'buat surat persetujuan', 'buat rekomendasi', "
            "'siapkan surat keputusan', atau setelah validasi positif dan "
            "petugas siap menyetujui. BUKAN tindakan menulis ke SIAP — hanya "
            "draf bantu; dokumen final tetap diterbitkan ber-TTE oleh SIAP saat "
            "berkas disetujui, jadi TIDAK perlu konfirmasi. Tidak butuh argumen "
            "— konteks izin, langkah, & pemohon sudah terikat ke sesi."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "fill_siap_form",
        "description": (
            "ISIKAN Formulir Isian permohonan LANGSUNG di SIAP — field teks DAN "
            "lampiran berkas ke slot-slotnya — lalu pandu petugas MEMERIKSA. "
            "SIAP membuat SK dari formulir yang terisi ini, jadi tool inilah yang "
            "menyiapkan data untuk SK di SIAP (berbeda dari draft_sk yang hanya "
            "mengirim draf PDF/.docx bantu ke chat). Gunakan saat petugas minta "
            "'isikan formulirnya', 'isi Formulir Isian', 'isi form permohonan', "
            "'siapkan/isikan data untuk SK di SIAP', atau setelah review positif "
            "dan petugas siap menyetujui. Mengisi HANYA dari sumber nyata: profil "
            "pemohon SIAP + data permohonan + hasil baca dokumen unggahan "
            "(dibedakan mana yang perlu diperiksa); field yang tidak terbaca "
            "dibiarkan kosong (tidak pernah dikarang) untuk dilengkapi petugas. "
            "BUKAN tindakan keputusan — hanya menyiapkan isian formulir, jadi "
            "TIDAK perlu konfirmasi. Tidak butuh argumen — konteks izin, "
            "permohonan, & pemohon sudah terikat ke sesi."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "set_ppkp_number",
        "description": (
            "TULISKAN No. PPKP (nomor izin PPKP yang DIBERIKAN PETUGAS pada tahap "
            "Penomoran) ke form Penomoran permohonan di SIAP. No. PPKP TIDAK berada "
            "di Formulir Isian pemohon — ia ada di form Penomoran (field no_ppkp), "
            "yang ditemukan otomatis, bukan di-hardcode. Gunakan saat petugas minta "
            "'isikan nomor ppkp', 'nomor ppkp = X', 'isikan nomor ppkp yaitu X', "
            "'tulis No. PPKP-nya', atau menyebut nomor PPKP yang harus diisi. BIMA "
            "menulis PERSIS nilai yang petugas berikan — TIDAK PERNAH mengarang "
            "nomor izin. Bila petugas belum menyebut nomornya, MINTA dulu (jangan "
            "menulis apa pun). BUKAN tindakan keputusan/teruskan — hanya menulis "
            "isian, jadi TIDAK perlu konfirmasi. SIAP menerbitkan SK final ber-TTE "
            "dari form itu; petugas cukup memeriksa lalu Simpan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nomor_ppkp": {
                    "type": "string",
                    "description": (
                        "Nomor PPKP yang diberikan petugas, PERSIS seperti "
                        "diucapkan (mis. \"2026/1234/01\"). Ditulis apa adanya ke "
                        "field no_ppkp form Penomoran — tidak dikarang/diubah."
                    ),
                },
            },
            "required": ["nomor_ppkp"],
        },
    },
    {
        "name": "compare_field",
        "description": (
            "Bandingkan satu field di antara dua dokumen yang sudah "
            "diekstrak (mis. NIK di KTP vs NIK di NIB). Mengembalikan "
            "kesamaan persis, skor kemiripan 0-1, dan catatan singkat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_a": {
                    "type": "object",
                    "description": "Dokumen pertama (dict field → nilai).",
                },
                "doc_b": {
                    "type": "object",
                    "description": "Dokumen kedua (dict field → nilai).",
                },
                "field": {
                    "type": "string",
                    "description": (
                        "Nama field yang akan dibandingkan (mis. 'nama', "
                        "'nik', 'alamat')."
                    ),
                },
            },
            "required": ["doc_a", "doc_b", "field"],
        },
    },
    {
        "name": "compare_identity",
        "description": (
            "Bandingkan field IDENTITAS (NIK atau nama) antara DUA dokumen yang "
            "diunggah — fitur andalan verifikasi. Gunakan saat petugas minta "
            "mencocokkan NIK/nama antar berkas, mis. 'bandingkan NIK di KTP "
            "dengan surat permohonan', 'cocokkan nama di KTP dan surat "
            "permohonan'. Tool ini MEMBACA sendiri kedua dokumen (Gemini "
            "Vision) lalu membandingkan — jadi TIDAK perlu memanggil "
            "get_doc_summary atau compare_field lebih dulu. `doc_a`/`doc_b` "
            "adalah jenis/label dokumen (mis. 'KTP', 'surat permohonan') atau "
            "file_id; `field` = 'nik' atau 'nama'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_a": {
                    "type": "string",
                    "description": (
                        "Dokumen pertama — jenis/label (mis. 'KTP') atau file_id."
                    ),
                },
                "doc_b": {
                    "type": "string",
                    "description": (
                        "Dokumen kedua — jenis/label (mis. 'surat permohonan') "
                        "atau file_id."
                    ),
                },
                "field": {
                    "type": "string",
                    "description": "Field identitas: 'nik' atau 'nama'.",
                },
            },
            "required": ["doc_a", "doc_b", "field"],
        },
    },
    {
        "name": "cite_regulation",
        "description": (
            "Cari kutipan peraturan OSS-RBA dari ChromaDB. Mengembalikan "
            "tiga chunk paling relevan dengan judul, cuplikan, dan sumber. "
            "Wajib digunakan ketika petugas bertanya tentang persyaratan, "
            "kewajiban, atau dasar hukum."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Kata kunci pencarian dalam Bahasa Indonesia, mis. "
                        "'persyaratan KBLI 56101 risiko rendah'."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_case_log_notes",
        "description": (
            "Ambil rangkaian catatan dari meja/tahap SEBELUMNYA pada riwayat "
            "berkas (license_log SIAP). Gunakan saat petugas baru membuka "
            "kasus agar tahu apa yang dikatakan petugas sebelumnya — konteks "
            "serah-terima antar meja."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit (mis. '000077591').",
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "get_siap_signing_link",
        "description": (
            "HANYA untuk mode asisten tanda tangan. Ambil tautan halaman "
            "tanda tangan SIAP Jateng untuk berkas ini, supaya Kepala "
            "DPMPTSP dapat klik dan menandatangani izin di SIAP. BIMA TIDAK "
            "menandatangani — penandatanganan (TTE/BSRE) dilakukan di SIAP. "
            "Gunakan tool ini saat Kepala siap menandatangani atau bertanya "
            "cara/tempat tanda tangan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit (mis. '000077591').",
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "forward_case",
        "description": (
            "TINDAKAN MENGUBAH DATA. Teruskan berkas ke meja persetujuan "
            "berikutnya di SIAP. WAJIB pola dua langkah: panggil pertama "
            "dengan confirmed=false untuk mendapatkan DRAF, tampilkan draf "
            "itu ke petugas, lalu panggil lagi dengan confirmed=true HANYA "
            "setelah petugas menjawab 'ya'/'setuju' secara eksplisit di "
            "giliran tepat sebelumnya. Permintaan awal petugas BUKAN "
            "konfirmasi."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit, berkas yang diteruskan.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "Catatan petugas (opsional, maks 1000 karakter) yang "
                        "dicatat ke license_log agar dibaca meja berikutnya."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Set true HANYA setelah petugas mengonfirmasi "
                        "eksplisit. Jika false/diabaikan, tool hanya "
                        "mengembalikan draf dan tidak mengubah data apa pun."
                    ),
                },
            },
            "required": ["ticket"],
        },
    },
    {
        "name": "record_decision",
        "description": (
            "TINDAKAN MENGUBAH DATA — paling penting. Catat keputusan "
            "petugas TERIMA (approved) atau TOLAK (rejected) atas izin di "
            "SIAP. WAJIB pola dua langkah: panggil pertama dengan "
            "confirmed=false untuk DRAF, tampilkan ke petugas, lalu panggil "
            "lagi dengan confirmed=true HANYA setelah petugas menjawab 'ya' "
            "secara eksplisit di giliran tepat sebelumnya. Keputusan izin "
            "adalah tanggung jawab petugas, bukan BIMA."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Nomor tiket SIAP, 9 digit.",
                },
                "decision": {
                    "type": "string",
                    "description": (
                        "Keputusan: 'approved' (terima) atau 'rejected' (tolak)."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Alasan/keterangan keputusan dari petugas — WAJIB "
                        "diisi, maks 1000 karakter. Jangan dikarang; ambil "
                        "dari petugas atau dari temuan validasi nyata."
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Set true HANYA setelah petugas mengonfirmasi "
                        "eksplisit. Jika false/diabaikan, tool hanya "
                        "mengembalikan draf dan tidak mengubah data apa pun."
                    ),
                },
            },
            "required": ["ticket", "decision", "notes"],
        },
    },
]


# Map tool name → callable. Each callable is invoked with the dict of
# arguments the model produced. The chat loop handles sync vs async.
_TOOL_DISPATCH: dict[str, Any] = {
    "get_validation_summary": get_validation_summary,
    "get_case_full": get_case_full,
    "get_doc_summary": get_doc_summary,
    "send_document": send_document,
    "draft_sk": draft_sk,
    "fill_siap_form": fill_siap_form,
    "set_ppkp_number": set_ppkp_number,
    "compare_field": compare_field,
    "compare_identity": compare_identity,
    "cite_regulation": cite_regulation,
    "get_case_log_notes": get_case_log_notes,
    "get_siap_signing_link": get_siap_signing_link,
    "forward_case": forward_case,
    "record_decision": record_decision,
}


# ---------------------------------------------------------------------------
# Per-mode tool exposure.
#
# The function-calling agent is shared; what differs per mode is WHICH tools
# the model may call. The signature-assistant (Wave 4) is read-only over the
# case and gets the SIAP signing handoff tool — it deliberately does NOT see
# `forward_case`/`record_decision`, because the only accountable action at the
# signing stage is the signature itself, which happens in SIAP, not BIMA.
# ---------------------------------------------------------------------------

# Tools the officer copilot may call — the full set (Wave 1-2 behaviour).
_OFFICER_TOOL_NAMES = frozenset({
    "get_validation_summary",
    "get_case_full",
    "get_doc_summary",
    "send_document",
    "draft_sk",
    "fill_siap_form",
    "set_ppkp_number",
    "compare_field",
    "compare_identity",
    "cite_regulation",
    "get_case_log_notes",
    "forward_case",
    "record_decision",
})

# Tools the signature-assistant may call — read-only chain synthesis plus the
# SIAP signing deep-link handoff. No write tools.
_SIGNATURE_TOOL_NAMES = frozenset({
    "get_validation_summary",
    "get_case_full",
    "cite_regulation",
    "get_case_log_notes",
    "get_siap_signing_link",
})


def _declarations_for_mode(mode: str) -> list[dict[str, Any]]:
    """Return the Gemini functionDeclarations subset allowed in `mode`."""
    allowed = (
        _SIGNATURE_TOOL_NAMES if mode == _MODE_SIGNATURE else _OFFICER_TOOL_NAMES
    )
    return [d for d in _FUNCTION_DECLARATIONS if d["name"] in allowed]


def _result_preview(value: Any, limit: int = 240) -> str:
    """Compact one-line preview of a tool result for transcripts/logs."""
    try:
        s = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(value)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


async def _invoke_tool(name: str, args: dict[str, Any]) -> Any:
    """Run one tool, awaiting if async. Catches exceptions so the model
    always gets a structured result back (never an unhandled raise)."""
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"Tool {name!r} tidak dikenal."}
    try:
        result = fn(**args) if not _is_coroutine_function(fn) else await fn(**args)
        return result
    except TypeError as e:
        logger.warning("Tool %s called with bad args=%s | err=%s", name, args, e)
        return {"error": f"Argumen tidak valid untuk {name}: {e}"}
    except Exception as e:  # pragma: no cover — defensive
        logger.exception("Tool %s raised", name)
        return {"error": f"Tool {name} gagal: {e}"}


def _is_coroutine_function(fn: Any) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


# ---------------------------------------------------------------------------
# OfficerCopilot — the main class.
# ---------------------------------------------------------------------------


class OfficerCopilot:
    """
    Officer-facing AI partner. One instance per process is fine; the class
    holds no per-request state. The `chat` method is fully self-contained.
    """

    def __init__(
        self,
        api_key: str = _GEMINI_API_KEY,
        model: str = _GEMINI_TOOL_MODEL,
        timeout: float = _TOOL_TIMEOUT_SECONDS,
        max_rounds: int = _MAX_TOOL_ROUNDS,
    ) -> None:
        self.api_key = api_key
        self.model = model.removeprefix("models/")
        self.timeout = timeout
        self.max_rounds = max_rounds

    def _endpoint(self) -> str:
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )

    def _build_initial_contents(
        self,
        message: str,
        history: list[dict],
    ) -> list[dict]:
        """
        Convert the caller's history (list of {role, text}) plus the new
        user message into Gemini's `contents` array. Gemini accepts roles
        `user` and `model`; any unknown role is coerced to `user`.
        """
        contents: list[dict] = []
        for turn in history or []:
            role = turn.get("role", "user")
            text = turn.get("text", "")
            if not text:
                continue
            if role not in ("user", "model"):
                role = "user"
            contents.append({"role": role, "parts": [{"text": text}]})
        contents.append({"role": "user", "parts": [{"text": message}]})
        return contents

    async def chat(
        self,
        message: str,
        ticket: str,
        history: list[dict],
        officer_id: int | None = None,
        validation: dict | None = None,
        mode: str = _MODE_OFFICER,
        documents: dict | None = None,
        sk_context: dict | None = None,
    ) -> dict:
        """
        Run one user-message turn. Returns:
          {
            "reply":       str (final assistant text),
            "tool_calls":  list of {name, args, result_preview},
            "history":     updated history including the new user turn
                           and the final model reply, ready to be passed
                           back on the next call.
          }

        Args:
          officer_id: the verified-JWT officer this session belongs to. Used
            only for masked logging — the agent holds no per-officer state;
            durability is admin-api's `copilot_session` table. NEVER trust an
            officer_id from a request body; admin-api derives it from the JWT.
          validation: the BIMA validator result for `ticket` (score, status,
            summary, issues), computed by admin-api and injected here so the
            `get_validation_summary` tool can surface it without re-running
            Gemini Vision. None when no validation is available yet.
          mode: "officer" (default) for the validation-first desk copilot, or
            "signature" for the Head-of-DPMPTSP signing assistant (Wave 4 /
            Vision req #13). The mode selects the system prompt and the
            tool subset; the agent machinery is otherwise identical. An
            unknown value falls back to "officer".
          documents: optional in-session document bytes keyed by file_id —
            {file_id: {"filename", "mime_type", "content" (bytes),
            "claimed_type"}}. Supplied by the chat bridge (officer_bridge.py)
            in the WhatsApp/Telegram demo where the citizen sent their docs
            straight into BIMA. Lets `get_doc_summary` answer with real Gemini
            Vision instead of the canned placeholder. None on the admin
            dashboard path (SIAP file-download not wired). Bytes never logged.
          sk_context: optional SK-draft context for `draft_sk` — {"license_id",
            "ticket", "applicant_name", "alamat", "license_name"}. Supplied by
            the chat bridge so the officer deputy can render SIAP's PKPP
            approval-letter template. None on the admin path (draft_sk then says
            so plainly). Applicant fields go into the SK docx (authorized
            officer) but are never logged.
        """
        mode = mode if mode in _VALID_MODES else _MODE_OFFICER

        if not is_configured():
            unavailable = (
                "Maaf, Asisten Tanda Tangan belum dapat digunakan: "
                if mode == _MODE_SIGNATURE
                else "Maaf, Officer Copilot belum dapat digunakan: "
            )
            return {
                "reply": (
                    unavailable
                    + "GEMINI_API_KEY belum dikonfigurasi di server ai-engine."
                ),
                "tool_calls": [],
                "history": list(history or []),
            }

        contents = self._build_initial_contents(message, history)
        tool_calls_log: list[dict[str, Any]] = []

        prompt_template = (
            _SIGNATURE_SYSTEM_PROMPT_TEMPLATE
            if mode == _MODE_SIGNATURE
            else _SYSTEM_PROMPT_TEMPLATE
        )
        system_instruction = {
            "role": "system",
            "parts": [{"text": prompt_template.format(ticket=ticket)}],
        }

        # Bind the validation result + any in-session documents for this turn
        # so `get_validation_summary` / `get_doc_summary` can read them. The
        # token resets in `finally` keep concurrent requests isolated — each
        # asyncio task gets its own ContextVar copy. `_docs_to_send_context`
        # is a fresh per-turn list `send_document` appends resolved file_ids
        # to; we drain it into the result so the bridge can push the files.
        ctx_token = _validation_context.set(validation)
        doc_token = _doc_context.set(documents)
        send_token = _docs_to_send_context.set([])
        sk_token = _sk_context.set(sk_context)
        logger.info(
            "Copilot turn start | mode=%s | officer_id=%s | ticket=%s | "
            "has_validation=%s | in_session_docs=%d | history_turns=%d",
            mode,
            officer_id if officer_id is not None else "<none>",
            ticket,
            validation is not None,
            len(documents or {}),
            len(history or []),
        )
        try:
            result = await self._run_chat_loop(
                client_message=message,
                ticket=ticket,
                history=history,
                contents=contents,
                tool_calls_log=tool_calls_log,
                system_instruction=system_instruction,
                mode=mode,
            )
            # Surface any documents `send_document` resolved this turn so the
            # bridge can deliver them out-of-band. Always a list (possibly
            # empty). Only meaningful in officer mode (send_document is not in
            # the signature tool set), but harmless elsewhere.
            queued = _docs_to_send_context.get() or []
            result["documents_to_send"] = list(queued)
            return result
        finally:
            _validation_context.reset(ctx_token)
            _doc_context.reset(doc_token)
            _docs_to_send_context.reset(send_token)
            _sk_context.reset(sk_token)

    async def _run_chat_loop(
        self,
        *,
        client_message: str,
        ticket: str,
        history: list[dict],
        contents: list[dict],
        tool_calls_log: list[dict[str, Any]],
        system_instruction: dict,
        mode: str = _MODE_OFFICER,
    ) -> dict:
        """The Gemini function-calling round loop. Split out of `chat()` so the
        ContextVar set/reset stays a tight wrapper around the whole turn."""
        message = client_message
        # Only the tools allowed in `mode` are declared to Gemini — the
        # signature-assistant never sees the write tools.
        declarations = _declarations_for_mode(mode)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for round_idx in range(self.max_rounds):
                payload = {
                    "systemInstruction": system_instruction,
                    "contents": contents,
                    "tools": [{"functionDeclarations": declarations}],
                    "generationConfig": {
                        "temperature": 0.2,
                        "maxOutputTokens": 1024,
                    },
                }

                try:
                    resp = await client.post(self._endpoint(), json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.TimeoutException:
                    logger.warning(
                        "Copilot Gemini timeout | round=%d | model=%s",
                        round_idx, self.model,
                    )
                    return self._error_reply(
                        "Permintaan ke Gemini melewati batas waktu.",
                        tool_calls_log, history, message,
                    )
                except httpx.HTTPStatusError as e:
                    logger.warning(
                        "Copilot Gemini HTTP %d | round=%d | body=%s",
                        e.response.status_code, round_idx,
                        e.response.text[:300],
                    )
                    return self._error_reply(
                        f"Gemini merespon HTTP {e.response.status_code}.",
                        tool_calls_log, history, message,
                    )
                except httpx.RequestError as e:
                    logger.warning("Copilot Gemini network error | err=%s", e)
                    return self._error_reply(
                        "Gangguan jaringan saat menghubungi Gemini.",
                        tool_calls_log, history, message,
                    )

                candidate = (data.get("candidates") or [{}])[0]
                content = candidate.get("content") or {}
                parts = content.get("parts") or []

                function_calls = [
                    p["functionCall"] for p in parts if "functionCall" in p
                ]
                text_chunks = [p.get("text", "") for p in parts if "text" in p]

                # Final text reply (no tool call) → done.
                if not function_calls:
                    reply_text = "".join(text_chunks).strip()
                    if not reply_text:
                        # A mis-parse must NEVER dead-end (rehearsal: "saya mau
                        # liat surat permohonannya" → blank reply). Offer a
                        # concrete capability menu so the officer always has a
                        # next step — mode-aware, so the signing desk isn't
                        # promised officer-only tools.
                        reply_text = _empty_reply_fallback(mode)
                    new_history = list(history or [])
                    new_history.append({"role": "user", "text": message})
                    new_history.append({"role": "model", "text": reply_text})
                    return {
                        "reply": reply_text,
                        "tool_calls": tool_calls_log,
                        "history": new_history,
                    }

                # Otherwise: execute every requested tool call, append both
                # the model's functionCall and our functionResponse to
                # contents, then loop.
                contents.append({"role": "model", "parts": parts})

                response_parts: list[dict[str, Any]] = []
                allowed_tools = (
                    _SIGNATURE_TOOL_NAMES
                    if mode == _MODE_SIGNATURE
                    else _OFFICER_TOOL_NAMES
                )
                for fc in function_calls:
                    name = fc.get("name", "")
                    args = fc.get("args") or {}
                    logger.info(
                        "Copilot tool call | mode=%s | round=%d | name=%s | args=%s",
                        mode, round_idx, name, _result_preview(args, 160),
                    )
                    # Defence-in-depth: even though only mode-scoped tools are
                    # declared to Gemini, refuse to EXECUTE a tool outside the
                    # mode's allowed set. A signature-assistant turn can never
                    # run a write tool, period.
                    if name not in allowed_tools:
                        logger.warning(
                            "Copilot BLOCKED out-of-mode tool | mode=%s | name=%s",
                            mode, name,
                        )
                        blocked = {
                            "error": (
                                f"Tool {name!r} tidak tersedia dalam mode "
                                f"{mode!r}."
                            )
                        }
                        tool_calls_log.append({
                            "name": name,
                            "args": args,
                            "result_preview": _result_preview(blocked),
                        })
                        response_parts.append({
                            "functionResponse": {
                                "name": name,
                                "response": {"result": blocked},
                            },
                        })
                        continue
                    # Defence-in-depth audit line: a confirmed write is an
                    # accountable action — log it loudly, separately from the
                    # generic tool-call line, with PII-light args only.
                    if name in _REQUIRES_CONFIRMATION and args.get("confirmed") is True:
                        logger.warning(
                            "Copilot CONFIRMED WRITE | name=%s | ticket=%s | "
                            "decision=%s | note_len=%d",
                            name,
                            args.get("ticket", "<none>"),
                            args.get("decision", "-"),
                            len(str(args.get("notes") or args.get("note") or "")),
                        )
                    result = await _invoke_tool(name, args)
                    preview = _result_preview(result)
                    logger.info(
                        "Copilot tool result | name=%s | result=%s",
                        name, preview,
                    )
                    tool_calls_log.append({
                        "name": name,
                        "args": args,
                        "result_preview": preview,
                    })
                    response_parts.append({
                        "functionResponse": {
                            "name": name,
                            "response": {"result": result},
                        },
                    })

                contents.append({"role": "user", "parts": response_parts})

        # Loop exhausted without a text reply. Surface what we have so the
        # officer is not left hanging.
        logger.warning(
            "Copilot exceeded max_rounds=%d without final reply | ticket=%s",
            self.max_rounds, ticket,
        )
        return self._error_reply(
            (
                f"Saya sudah memanggil tool {self.max_rounds} kali tanpa bisa "
                "menyusun jawaban akhir. Mohon persempit pertanyaan Anda."
            ),
            tool_calls_log, history, message,
        )

    def _error_reply(
        self,
        reason: str,
        tool_calls_log: list[dict],
        history: list[dict],
        message: str,
    ) -> dict:
        new_history = list(history or [])
        new_history.append({"role": "user", "text": message})
        new_history.append({"role": "model", "text": reason})
        return {
            "reply": reason,
            "tool_calls": tool_calls_log,
            "history": new_history,
        }


# Module-level singleton for callers that want a default instance.
_default: Optional[OfficerCopilot] = None


def get_copilot() -> OfficerCopilot:
    global _default
    if _default is None:
        _default = OfficerCopilot()
    return _default
