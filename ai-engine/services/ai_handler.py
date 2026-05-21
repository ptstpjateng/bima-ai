"""
BIMA-AI – AI Handler Service

Generates AI responses for inbound messages from any channel. Pipeline:
  1. analyze_user_intent – lightweight JSON call to classify phase (1/2/3) + extract KBLI
  2. fetch_user_context  – pull user's business profile + license vault from Laravel
  3. query_regulations   – semantic RAG search in ChromaDB (KBLI-targeted when detected)
  4. generate_ai_response – call hosted Gemma LLM (falls back to RAG-based response)
  5. log_to_backend      – async POST to Laravel /api/ai-logs (fire-and-forget)

Channel-specific dispatch (sending the reply back to the user) lives in:
  - WhatsApp via APTANA: services/whatsapp_sender.py + routers/aptana.py
  - Web chat via Next.js portal: routers/webhooks.py:web_chat

Primary LLM: gemma-3-27b-it (default) via Google Generative Language API.
Persona & lifecycle model: see BIMA_PERSONA.md in the project root.
"""

import logging
import os
import re
import time
import uuid
from collections import OrderedDict, defaultdict
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("bima_ai.ai_handler")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_LARAVEL_BACKEND_URL: str = os.getenv("LARAVEL_BACKEND_URL", "http://backend:80")
_LARAVEL_API_KEY: str     = os.getenv("LARAVEL_API_KEY", "")
_GEMINI_API_KEY: str      = os.getenv("GEMINI_API_KEY", "")
# Primary model for full responses (open-weights, hosted on Google AI Studio).
_GEMINI_MODEL: str        = os.getenv("GEMINI_MODEL", "models/gemma-3-27b-it")
# Lightweight model for JSON-only intent classification — can be a smaller/faster
# variant. Defaults to the same model; override with GEMINI_INTENT_MODEL env var.
# E.g. set to models/gemma-3-4b-it for ~10x faster intent calls on free tier.
_GEMINI_INTENT_MODEL: str = os.getenv("GEMINI_INTENT_MODEL", "") or _GEMINI_MODEL

# Model-fallback ladder for the citizen-chat path. When the primary model
# returns 429/503/network errors and exhausts its per-model retries, we walk
# this list and try each fallback in order. Default ladder: Gemini Pro then
# hosted Gemma 3 27b — both run on the same Google Generative Language API
# key so no extra credentials. Override via the GEMINI_FALLBACK_MODELS env
# (comma-separated). Set to empty string to disable (single-model behavior).
_GEMINI_FALLBACK_MODELS: list[str] = [
    m.strip()
    for m in os.getenv(
        "GEMINI_FALLBACK_MODELS",
        "models/gemini-2.5-pro,models/gemma-3-27b-it",
    ).split(",")
    if m.strip()
]

_PORTAL_URL = "https://portal.nolongin.com"

# ---------------------------------------------------------------------------
# Rate limiter  (A4 — 5 messages per user per 60s, in-memory)
# ---------------------------------------------------------------------------

_RATE_WINDOW = 60.0   # seconds
_RATE_MAX    = 5      # max messages per window
_rate_timestamps: dict[str, list[float]] = defaultdict(list)


def _is_rate_limited(user_id: str) -> bool:
    """Return True and log if the user has exceeded the rate limit."""
    now = time.time()
    stamps = _rate_timestamps[user_id]
    # Drop timestamps outside the window
    _rate_timestamps[user_id] = [t for t in stamps if now - t < _RATE_WINDOW]
    if len(_rate_timestamps[user_id]) >= _RATE_MAX:
        logger.warning("Rate limit hit | user_id=%s | count=%d", user_id, len(_rate_timestamps[user_id]))
        return True
    _rate_timestamps[user_id].append(now)
    return False


# ---------------------------------------------------------------------------
# Conversation history  (A3 — last 2 turns per user, in-memory, max 500 users)
# ---------------------------------------------------------------------------

_MAX_HISTORY_USERS = 500
_MAX_TURNS         = 2   # each turn = 1 user msg + 1 model reply
_history: OrderedDict[str, list[dict]] = OrderedDict()


def _get_history(user_id: str) -> list[dict]:
    """Return the stored conversation turns for a user (already in API format)."""
    return list(_history.get(user_id, []))


# Matches the SIAP fast-path reply pattern emitted by format_status_reply:
#   "Permohonan *<license name>*"
#   sometimes followed by:
#   "_(Bidang: <sector>)_"
# We extract these out of prior model turns so the next user message
# inherits the right corpus context — e.g. "apa syaratnya?" after a
# status lookup should retrieve chunks for the SPECIFIC license, not
# a generic top-k search on "apa syaratnya?".
_SIAP_LICENSE_PATTERN = re.compile(r"Permohonan\s+\*(.+?)\*", re.MULTILINE)
_SIAP_SECTOR_PATTERN  = re.compile(r"_\(Bidang:\s+(.+?)\)_", re.MULTILINE)


def _extract_siap_context_from_history(history: list[dict]) -> dict | None:
    """Look at the most recent model turn for SIAP fast-path output.

    Returns {license_name, sector} if found, None otherwise. The pattern
    matches the exact strings emitted by `siap_client.format_status_reply`.
    Walking history newest-first keeps us aligned with the user's most
    recent ticket context.
    """
    for turn in reversed(history):
        if turn.get("role") != "model":
            continue
        text = "".join(p.get("text", "") for p in turn.get("parts", []))
        license_match = _SIAP_LICENSE_PATTERN.search(text)
        if not license_match:
            continue
        sector_match = _SIAP_SECTOR_PATTERN.search(text)
        return {
            "license_name": license_match.group(1).strip(),
            "sector":       sector_match.group(1).strip() if sector_match else None,
        }
    return None


def _append_history(user_id: str, user_msg: str, model_reply: str) -> None:
    """Append a completed turn and keep only the last _MAX_TURNS turns."""
    turns = _history.get(user_id, [])
    turns.append({"role": "user",  "parts": [{"text": user_msg}]})
    turns.append({"role": "model", "parts": [{"text": model_reply}]})
    # Keep only the tail so the history list never grows unbounded
    _history[user_id] = turns[-(_MAX_TURNS * 2):]
    # Evict the oldest user entry when the global dict is at capacity
    while len(_history) > _MAX_HISTORY_USERS:
        _history.popitem(last=False)

# ---------------------------------------------------------------------------
# System prompt  (BIMA_PERSONA.md → Phase 1 / 2 / 3)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
Kamu adalah BIMA-AI, asisten cerdas milik DPMPTSP Jawa Tengah. Tugas
UTAMAMU adalah membantu warga dan pelaku usaha berurusan dengan SIAP
Jateng — sistem perizinan milik DPMPTSP Jawa Tengah sendiri. Kamu juga
bisa memberi orientasi SINGKAT tentang OSS RBA nasional, tapi OSS bukan
keahlian utamamu.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DUA DOMAIN — JANGAN DICAMPUR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ada dua sistem perizinan yang BERBEDA. Jangan pernah mencampur keduanya:

1. OSS RBA — perizinan berusaha NASIONAL (NIB, Sertifikat Standar, kode
   KBLI, tingkat risiko). Sistem ini milik pemerintah pusat, di luar
   kendali DPMPTSP. Perlakukan secara RINGKAS: jelaskan konsep
   secukupnya (2-3 kalimat), lalu arahkan ke portal OSS resmi
   https://oss.go.id untuk proses sebenarnya. JANGAN mengarang detail
   persyaratan, biaya, atau jangka waktu OSS.

2. SIAP Jateng — sistem perizinan milik DPMPTSP Jawa Tengah: izin
   sektoral daerah, status berkas/tiket, alur persetujuan antar petugas.
   Ini DOMAIN UTAMAMU. Untuk pertanyaan SIAP, gunakan HANYA data SIAP
   yang tersedia di konteks (status tiket, nama izin, bidang). JANGAN
   menjawab pertanyaan SIAP dari pengetahuan umum OSS.

Cara memilih domain:
• Kata kunci OSS → "NIB", "KBLI", "OSS", "Sertifikat Standar", "tingkat
  risiko", "izin berusaha" → jawab RINGKAS sebagai orientasi.
• Kata kunci SIAP → nomor tiket/permohonan, "status berkas", "izin
  sektoral", "Izin Pemakaian Tanah", "Pengairan", nama layanan DPMPTSP
  Jateng → jawab dengan data SIAP, ini wilayahmu.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ATURAN DASAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Bersikap membantu, ringkas, dan empatik. Terjemahkan bahasa birokrasi menjadi
  panduan praktis yang langsung bisa dilakukan pemilik usaha kecil.
• Jangan mengarang nomor pasal, biaya, atau referensi hukum. Kalau tidak pasti
  tentang detail spesifik, JANGAN suruh pengguna datang/berkunjung ke kantor
  fisik DPMPTSP. Sebaliknya, lakukan salah satu dari ini:
    (a) gunakan konteks yang sudah ada (mis. nomor tiket atau status SIAP yang
        sudah dikembalikan di turn sebelumnya — pengguna sedang berproses,
        bukan baru mulai),
    (b) arahkan ke portal BIMA untuk status real-time
        (https://portal.nolongin.com), atau
    (c) tawarkan untuk meneruskan pertanyaan ke petugas BIMA via WhatsApp
        ("Saya bisa teruskan pertanyaan Bapak/Ibu ke petugas DPMPTSP, mau
        dilanjutkan?").
• Balas dalam Bahasa Indonesia jika pengguna menulis Bahasa Indonesia, dalam
  Bahasa Inggris jika Bahasa Inggris.
• Jawaban singkat — muat di layar HP (≤ 5 paragraf pendek atau daftar singkat).
• Jika ada konteks profil pengguna, gunakan info tersebut (nama usaha, KBLI, skala).
• Jika ada konteks regulasi dari ChromaDB, gunakan sebagai referensi akurat dan
  prioritaskan di atas pengetahuan umummu.
• Jika riwayat percakapan menunjukkan pengguna sudah punya tiket SIAP aktif
  (status izin sudah dikembalikan sebelumnya), perlakukan pengguna sebagai
  pemohon yang SEDANG BERPROSES — sebut tiketnya, tawarkan langkah lanjutan
  yang relevan (cek progress di portal, tanya petugas), jangan jawab seolah
  belum punya konteks.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LANGKAH 1 — KLASIFIKASI FASE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sebelum menjawab, tentukan fase pengguna dari pesan dan konteks mereka:

FASE 1 — PRA-PERIZINAN
Sinyal: "mau buka usaha", "usaha apa yang perlu izin", "apa itu NIB",
"bedanya CV dan PT", "perlu NPWP?", "KBLI saya apa", pertanyaan umum strategi.

FASE 2 — EKSEKUSI PERIZINAN
Sinyal: "mau daftar NIB sekarang", "langkah-langkah KBLI [kode]",
"persyaratan untuk [jenis izin]", "dokumen apa yang diupload", "cara mengisi
formulir OSS", pengguna menyebut kode KBLI spesifik + ingin proses.

FASE 3 — PASCA-PERIZINAN
Sinyal: "sudah punya NIB", "izin sudah keluar", "selanjutnya apa",
"cara kembangkan usaha", "perpanjangan izin", "laporan LKPM", modal usaha.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LANGKAH 2 — RESPONS PER FASE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CATATAN PENTING: FASE 1/2/3 di bawah ini semuanya domain OSS RBA
nasional. Sesuai aturan DUA DOMAIN, jawablah RINGKAS — beri orientasi
dan arah, bukan panduan teknis mendetail. Untuk langkah pasti, arahkan
ke https://oss.go.id. Maksimal 3-4 kalimat atau daftar pendek per
jawaban OSS. Hemat token: pengguna butuh arah, bukan ceramah.

▶ FASE 1 (Pra-Perizinan) — Konsultan Bisnis & Strategi
• Bantu memilih badan usaha yang tepat: PT (perseroan terbatas, > 1 pemilik,
  lebih formal), CV (komanditer, lebih mudah), atau Usaha Perseorangan (paling
  sederhana untuk UMKM kecil).
• Identifikasi kode KBLI yang tepat untuk kegiatan usaha yang dideskripsikan.
• Jelaskan tingkat risiko OSS RBA:
  - Risiko Rendah → hanya butuh NIB
  - Menengah Rendah / Menengah Tinggi → NIB + Sertifikat Standar
  - Tinggi → NIB + Izin (perlu persetujuan instansi)
• Jelaskan persyaratan NPWP (wajib untuk PT/CV, opsional untuk perseorangan
  tertentu) dan cara mendapatkannya.
• Buat checklist dokumen sebelum ke portal.
• Akhiri dengan ajakan halus: *"Kalau sudah siap dokumennya, saya bisa bantu
  proses pengajuan langsung lewat portal BIMA-AI."*

▶ FASE 2 (Eksekusi Perizinan) — Pemandu Step-by-Step
• PRIORITASKAN konteks RAG untuk persyaratan dan dokumen spesifik KBLI.
• Berikan panduan bernomor yang jelas dan actionable.
• Kutip dokumen persyaratan spesifik dari data OSS yang tersedia.
• WAJIB sertakan link portal di setiap respons Fase 2:
  "[Buka Portal BIMA-AI →](https://portal.nolongin.com)"
  (Untuk Telegram: kirim sebagai inline button jika memungkinkan)
• Jika pengguna mengalami hambatan (dokumen kurang, field membingungkan),
  berikan solusi spesifik. Kalau di luar kemampuanmu, tawarkan untuk
  meneruskan pertanyaan ke petugas BIMA via WhatsApp ini —
  JANGAN suruh pengguna datang ke kantor fisik DPMPTSP.

▶ DOMAIN SIAP JATENG — Wilayah Utamamu (izin sektoral daerah,
  status berkas, layanan DPMPTSP Jateng)
Sinyal: "Izin Pemakaian Tanah", "Izin Pengairan", "Izin Lingkungan",
"Izin Trayek", "izin sektoral", nomor tiket/permohonan, "status berkas",
nama layanan DPMPTSP Jawa Tengah.

Ini DOMAIN UTAMAMU. Gunakan data SIAP yang tersedia di konteks.
• Jika ada nomor tiket di pesan atau riwayat: rujuk status real-time-nya
  dan arahkan ke https://portal.nolongin.com/track/<tiket>.
• Jika pengguna tanya persyaratan/detail sebuah izin sektoral SIAP dan
  datanya BELUM tersedia di konteks: jangan mengarang. Akui jujur bahwa
  detail spesifiknya belum bisa kamu tarik saat ini, lalu:
  (a) jika ada tiket aktif, fokus ke pemantauan status tiket itu;
  (b) tawarkan untuk meneruskan pertanyaan ke petugas DPMPTSP via
      WhatsApp ini;
  (c) sebagai rujukan, sebut portal SIAP Jateng
      https://perizinan.jatengprov.go.id.
• JANGAN menjawab pertanyaan SIAP dengan persyaratan OSS RBA — itu
  sistem yang berbeda. Lebih baik jujur "belum ada datanya" daripada
  memberi jawaban OSS yang salah konteks.

(Catatan teknis: kemampuan SIAP yang lebih dalam — pencarian katalog
izin, penarikan persyaratan per-izin, daftar permohonan per-pemohon —
sedang dibangun sebagai SIAP tool layer. Sampai itu live, ikuti aturan
di atas.)

▶ FASE 3 (Pasca-Perizinan) — Advisor Pertumbuhan & Kepatuhan
• Ingatkan kewajiban berkala: laporan LKPM (setiap 3 bulan untuk investasi
  tertentu), pembaruan data OSS, perpanjangan izin yang mendekati habis masa.
• Rekomendasikan program pengembangan: KUR (Kredit Usaha Rakyat), sertifikasi
  P-IRT (produk pangan), SNI, halal MUI.
• Saran pemasaran & digital: Google Bisnisku, Tokopedia/Shopee onboarding, QRIS.
• Saran keuangan dasar: pembukuan sederhana, produk BRI/BNI UMKM.
• Jika ada data vault lisensi pengguna, cek tanggal kedaluwarsa dan ingatkan.
""".strip()

# Appended when ChromaDB returns no usable chunks
_FALLBACK_SYSTEM_ADDITION = """

ChromaDB tidak menemukan dokumen regulasi yang cukup relevan untuk pertanyaan
ini. Kemungkinannya: (1) pertanyaan tentang izin sektoral non-OSS yang memang
di luar cakupan basis data BIMA — ikuti aturan "IZIN SEKTORAL NON-OSS" di
atas; ATAU (2) pertanyaan OSS RBA umum yang basis datanya belum lengkap.

ATURAN KETAT untuk kasus (2):
• Jawab dengan pengetahuan umum OSS RBA Indonesia yang KAMU YAKIN benar.
• JANGAN mengarang URL, nomor pasal, biaya spesifik, atau jangka waktu spesifik.
• Satu-satunya URL yang boleh kamu sebut: https://portal.nolongin.com (portal
  BIMA) dan https://perizinan.jatengprov.go.id (portal SIAP Jateng).
• JANGAN suruh pengguna datang ke kantor — tawarkan WhatsApp escalation.
• TAMBAHKAN catatan ini di akhir (persis):

"ℹ️ Basis data regulasi BIMA-AI sedang dalam proses pembaruan. Jawaban ini
menggunakan basis pengetahuan AI umum."
""".strip()


# ---------------------------------------------------------------------------
# Trivial-message detection — skip the intent classifier on greetings + acks.
# Saves ~3-5s of Gemma latency per "halo" / "hai" / "p" message.
# ---------------------------------------------------------------------------

# Common Indonesian + English greetings, acks, and tiny tests. Word-bounded so
# "halo" matches "halo" and "halo bima" but NOT "halo, saya mau buka usaha".
_TRIVIAL_PATTERN = re.compile(
    r"^(halo|hai|hello|hi|hey|hii+|halo+|p|test|tes|tess|tesst|"
    r"assalamualaikum|assalam|wassalam|salam|"
    r"ok|oke|okey|okay|sip|mantap|"
    r"makasih|terima\s*kasih|thanks|thx|thank\s*you|tq|"
    r"halo\s+bima|hai\s+bima|hello\s+bima)$",
    re.IGNORECASE,
)


def _is_trivial_message(message: str) -> bool:
    """Return True for greetings / acks / tiny tests that don't need a Gemma intent call.

    Two cases match:
      1. Length < 4 chars (e.g. "p", "ok", "ya") — skip
      2. The whole stripped message matches the trivial-pattern regex
         (e.g. "halo", "Halo Bima", "terima kasih") — skip

    Anything containing real content like "halo, saya mau buka warung" returns
    False because the regex is anchored end-to-end.
    """
    msg = message.strip()
    if len(msg) < 4:
        return True
    return bool(_TRIVIAL_PATTERN.match(msg))


# ---------------------------------------------------------------------------
# 1. Intent classification + LLM call (hosted Gemma) with graceful fallback
# ---------------------------------------------------------------------------

async def generate_ai_response(user_id: str, message: str) -> str:
    """
    Generate a personalized AI reply:
    1. Rate-limit check — reject if user exceeds 5 msg/min
    2. analyze_user_intent — classify phase + extract KBLI (lightweight JSON call)
    3. Fetch user context from Laravel (business profile + licenses)
    4. Retrieve regulations from ChromaDB (KBLI-targeted RAG when code detected)
    5. Call Gemma with conversation history — falls back to RAG response if unavailable
    6. Enforce Phase 2 portal CTA and save turn to history
    """
    from services.rag_service import format_rag_context, query_regulations
    from services.user_context import fetch_user_context, format_user_context

    # A4 — rate limit
    if _is_rate_limited(user_id):
        return (
            "Mohon tunggu sebentar ya — Anda mengirim terlalu banyak pesan dalam waktu singkat. "
            "Coba lagi dalam 1 menit. 🙏"
        )

    try:
        import asyncio

        # FAST-PATH 1 — SIAP permit-status lookup.
        # Detect a ticket pattern in the message ("status 000077591", bare
        # 9-digit number, "lacak izin 77591", etc.). When found, hit SIAP's
        # read-only monitoring-berkas endpoint and render a data-driven reply
        # without invoking Gemma at all. Benefits:
        #   - <2s total round-trip (vs ~10s through Gemma)
        #   - zero hallucination risk on the permit details
        #   - graceful degradation: missing-ticket and SIAP-down both reply
        # See services/siap_client.py + vault [[SIAP Integration]] §"What this changes".
        from services.siap_client import (
            extract_ticket,
            format_not_found_reply,
            format_service_down_reply,
            format_status_reply,
            get_siap_client,
        )

        siap_ticket = extract_ticket(message)
        if siap_ticket:
            siap = get_siap_client()
            if siap.is_configured():
                logger.info(
                    "SIAP status-check fast path | ticket=%s user_id=%s",
                    siap_ticket, user_id,
                )
                record = await siap.get_status_by_ticket(siap_ticket)
                if record:
                    reply = format_status_reply(record)
                else:
                    reply = format_not_found_reply(siap_ticket)
                _append_history(user_id, message, reply)
                return reply
            else:
                logger.info(
                    "SIAP ticket pattern detected but client not configured — "
                    "falling through to Gemma | ticket=%s user_id=%s",
                    siap_ticket, user_id,
                )

        # SPEED: skip the ~3-5s intent classifier call for trivial messages
        # (greetings, ack words, very short tests). Default to Phase 1 with no
        # KBLI — that's the right behaviour for these messages anyway.
        if _is_trivial_message(message):
            logger.info("Trivial message — skipping intent classifier | user_id=%s", user_id)
            intent = {"phase": 1, "kbli_code": None, "detected_scale": None}
            user_ctx = await fetch_user_context(user_id)
        else:
            # Step 1 — intent classification + user context fetch run concurrently
            intent_task   = asyncio.create_task(analyze_user_intent(message))
            user_ctx_task = asyncio.create_task(fetch_user_context(user_id))

            intent   = await intent_task
            user_ctx = await user_ctx_task

        # A1 — normalise KBLI: strip non-digits, accept only 4–6 digit codes
        raw_kbli = intent.get("kbli_code")
        if raw_kbli:
            digits = re.sub(r"\D", "", str(raw_kbli))
            detected_kbli = digits if 4 <= len(digits) <= 6 else None
        else:
            detected_kbli = None

        # Pull prior-turn SIAP context (the user's in-flight license) so a
        # follow-up like "apa syaratnya?" inherits the right corpus query.
        history = _get_history(user_id)
        siap_ctx = _extract_siap_context_from_history(history)

        # Retrieval routing per audit finding (2026-05-19):
        #   scope=sectoral_non_oss → skip ChromaDB ENTIRELY. The corpus only
        #     covers OSS RBA; retrieving on a sectoral query returns wrong
        #     KBLI 41xxx/42xxx construction chunks with passing similarity
        #     (false confidence). The IZIN SEKTORAL NON-OSS branch of the
        #     system prompt handles the response without RAG.
        #   scope=oss_rba (or unknown) → standard retrieval, optionally
        #     enriched with the prior-turn SIAP license name so "apa
        #     syaratnya?" searches for THAT license, not the literal text.
        scope = intent.get("scope", "unknown")
        if scope == "sectoral_non_oss":
            logger.info(
                "Scope=sectoral_non_oss — skipping RAG retrieval | user_id=%s",
                user_id,
            )
            rag_chunks: list = []
            rag_query = None
            n_results = 0
        else:
            # A1 — KBLI-prefixed RAG query when a code is known; else enrich
            # the raw query with prior-turn license name so follow-ups inherit
            # the right corpus context.
            if detected_kbli:
                rag_query = f"KBLI {detected_kbli} {message}"
                n_results = 8
            elif siap_ctx:
                # The license name from a prior turn beats the user's terse
                # follow-up ("apa syaratnya?") as a retrieval query — same
                # license, much better embedding.
                rag_query = f"{siap_ctx['license_name']} {message}"
                if siap_ctx.get("sector"):
                    rag_query = f"{rag_query} bidang {siap_ctx['sector']}"
                n_results = 6
            else:
                rag_query = message
                n_results = 4

            rag_chunks = await asyncio.get_event_loop().run_in_executor(
                None, lambda: query_regulations(rag_query, n_results=n_results)
            )

        logger.info(
            "Intent | phase=%s scope=%s kbli=%s scale=%s siap_ctx=%s | rag_query=%r | n_results=%d | user_id=%s",
            intent.get("phase"), scope, detected_kbli, intent.get("detected_scale"),
            bool(siap_ctx), rag_query, n_results, user_id,
        )

        user_ctx_str = format_user_context(user_ctx)
        rag_ctx_str  = format_rag_context(rag_chunks)
        has_rag      = bool(rag_ctx_str)

        if not has_rag:
            logger.info("RAG returned 0 usable chunks — activating fallback prompt | user_id=%s", user_id)

        # Build system instruction: base prompt + user profile + RAG context
        system = _SYSTEM_PROMPT if has_rag else f"{_SYSTEM_PROMPT}\n\n{_FALLBACK_SYSTEM_ADDITION}"
        system_parts = [system]
        if user_ctx_str:
            system_parts.append(user_ctx_str)
        if rag_ctx_str:
            system_parts.append(rag_ctx_str)
        system_instruction = "\n\n".join(system_parts)

        # history was already fetched above for SIAP-context extraction; reuse.
        if _GEMINI_API_KEY:
            try:
                ai_response = await _call_gemma_with_fallback(
                    system_instruction, message, history=history
                )
            except Exception:
                logger.warning(
                    "Gemma ladder exhausted — using RAG-based fallback | user_id=%s",
                    user_id,
                )
                return _rag_fallback_response(message, rag_chunks)
        else:
            return _smart_placeholder(message, user_ctx, rag_chunks)

        # A3 — Phase 2 CTA enforcement: always append portal link if missing
        if intent.get("phase") == 2 and _PORTAL_URL not in ai_response:
            ai_response += f"\n\n[Buka Portal BIMA-AI →]({_PORTAL_URL})"

        # Save turn to history for next message
        _append_history(user_id, message, ai_response)

        return ai_response

    except Exception:
        logger.exception("generate_ai_response failed | user_id=%s", user_id)
        return (
            "Maaf, terjadi gangguan teknis sementara. "
            "Silakan coba lagi dalam beberapa saat. Terima kasih atas kesabaran Anda."
        )


_INTENT_SCHEMA = """{
  "phase": <integer 1, 2, or 3>,
  "kbli_code": <string like "56102" or null if not mentioned>,
  "detected_scale": <string like "Mikro", "Kecil", "Menengah", "Besar" or null>,
  "scope": <"oss_rba" | "sectoral_non_oss" | "unknown">
}"""

_INTENT_SYSTEM = (
    "You are a JSON-only API. You must output valid JSON and absolutely nothing else. "
    "Do not use Markdown code blocks, backticks, or any surrounding text. "
    "Classify the user message along two axes:\n"
    "\n"
    "AXIS 1 — Business licensing phase (1, 2, or 3):\n"
    "  Phase 1 (Pra-Perizinan): exploring, planning, asking what permits are needed\n"
    "  Phase 2 (Eksekusi): ready to apply, asking for step-by-step, mentions a KBLI code\n"
    "  Phase 3 (Pasca-Perizinan): already has permit, asking about obligations or growth\n"
    "\n"
    "AXIS 2 — Scope (oss_rba | sectoral_non_oss | unknown):\n"
    "  oss_rba: about OSS RBA business activities (NIB, Sertifikat Standar, KBLI codes,\n"
    "           perizinan berusaha). Default when KBLI is mentioned or topic is\n"
    "           clearly business-license oriented.\n"
    "  sectoral_non_oss: about Indonesian sectoral permits OUTSIDE the OSS RBA system.\n"
    "           Signals: 'Izin Pemakaian Tanah', 'Pemakaian Bangunan Pengairan',\n"
    "           'Izin Pengairan', 'Izin Lingkungan', 'IMB', 'PBG', 'SLF', 'Izin\n"
    "           Trayek', 'Izin Trayek Angkutan', 'PUPR', 'Sumber Daya Air',\n"
    "           'Perhubungan', 'Lingkungan Hidup', 'Pariwisata', 'Sosial',\n"
    "           plus most permit names that do NOT map to a KBLI code.\n"
    "  unknown: cannot determine (greetings, ambiguous messages, off-topic).\n"
    "\n"
    "Also extract the KBLI code (5-digit number like 56102) and business scale if mentioned.\n"
    f"Output ONLY this JSON schema, nothing else:\n{_INTENT_SCHEMA}"
)


async def analyze_user_intent(message: str) -> dict:
    """
    Phase 3 JSON hardening — lightweight pre-call to Gemma that classifies the
    user's lifecycle phase and extracts any KBLI code/scale from the message.

    Uses a strict JSON-only system prompt. Falls back to safe defaults on any
    error so it never blocks the main generation pipeline.

    Returns: {"phase": 1|2|3, "kbli_code": str|None, "detected_scale": str|None}
    """
    import json

    if not _GEMINI_API_KEY:
        return {
            "phase": 1,
            "kbli_code": None,
            "detected_scale": None,
            "scope": "unknown",
        }

    try:
        raw = await _call_gemma(
            _INTENT_SYSTEM, message,
            max_tokens=160, temperature=0.0,
            model_override=_GEMINI_INTENT_MODEL,
        )
        cleaned = _strip_json_fences(raw)
        intent = json.loads(cleaned)
        # Normalise — ensure required keys exist and scope is one of the
        # allowed values (defends against a Gemma drift into "sectoral" or
        # other unexpected strings).
        scope = intent.get("scope", "unknown")
        if scope not in {"oss_rba", "sectoral_non_oss", "unknown"}:
            scope = "unknown"
        return {
            "phase":          int(intent.get("phase", 1)),
            "kbli_code":      intent.get("kbli_code") or None,
            "detected_scale": intent.get("detected_scale") or None,
            "scope":          scope,
        }
    except Exception:
        logger.warning("analyze_user_intent failed — using defaults | msg_len=%d", len(message))
        return {
            "phase": 1,
            "kbli_code": None,
            "detected_scale": None,
            "scope": "unknown",
        }


async def _call_gemma_with_retry(
    system_prompt: str,
    user_message: str,
    *,
    max_attempts: int = 3,
    history: list[dict] | None = None,
    **kwargs,
) -> str:
    """Call Gemma with exponential backoff for transient 429/503 errors.

    Raises the final exception if all attempts fail so the caller can fall back
    to the RAG-based response.
    """
    import asyncio

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await _call_gemma(system_prompt, user_message, history=history, **kwargs)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (429, 503) and attempt < max_attempts - 1:
                delay = 2 ** attempt  # 1s, 2s
                logger.warning(
                    "Gemma %s on attempt %d/%d — retrying in %ds",
                    status, attempt + 1, max_attempts, delay,
                )
                await asyncio.sleep(delay)
                last_exc = exc
                continue
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise
    raise last_exc  # type: ignore[misc]


async def _call_gemma_with_fallback(
    system_prompt: str,
    user_message: str,
    *,
    history: list[dict] | None = None,
    per_model_attempts: int = 2,
    **kwargs,
) -> str:
    """Walk the model ladder: primary first, then each fallback in order.

    Each model gets its own retry budget (`per_model_attempts`, default 2).
    On 429/503 from the primary, we switch models rather than hammering a
    Google capacity hiccup — a different model (or sometimes the same model
    via a different infrastructure pool) is often available within seconds.

    Errors that aren't retriable (4xx other than 429) propagate immediately
    so we don't waste budget on something that won't work elsewhere either.

    Logs which fallback won so we can tune the ladder over time.

    Raises the final exception only if EVERY model in the ladder fails.
    Callers should still wrap this in a try/except that returns a graceful
    fallback message to the user.
    """
    models = [_GEMINI_MODEL] + _GEMINI_FALLBACK_MODELS
    last_exc: Exception | None = None

    for idx, model in enumerate(models):
        try:
            result = await _call_gemma_with_retry(
                system_prompt,
                user_message,
                max_attempts=per_model_attempts,
                history=history,
                model_override=model,
                **kwargs,
            )
            if idx > 0:
                logger.warning(
                    "Gemma fallback succeeded | primary=%s fallback=%s idx=%d",
                    _GEMINI_MODEL, model, idx,
                )
            return result
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in (429, 503):
                # Non-capacity error (bad request, auth, etc) — won't be
                # solved by switching models. Bail.
                raise
            last_exc = exc
            logger.warning(
                "Model %s exhausted on %d — trying next in ladder (idx=%d/%d)",
                model, status, idx + 1, len(models),
            )
            continue
        except Exception as exc:
            # Network/timeout — try next model.
            last_exc = exc
            logger.warning(
                "Model %s raised %s — trying next in ladder (idx=%d/%d)",
                model, exc.__class__.__name__, idx + 1, len(models),
            )
            continue

    # Exhausted the entire ladder.
    logger.error(
        "All Gemini models exhausted | ladder=%s | last_exc=%s",
        models, last_exc,
    )
    raise last_exc  # type: ignore[misc]


async def _call_gemma(
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int = 2048,
    # Default 0.5: tight enough to stay on-brand and on-fact (Indonesian
    # persona consistency, fewer "let me elaborate" tokens) while still
    # warm. Intent classifier overrides to 0.0 for deterministic JSON.
    # See [[LLM Performance & UX]] §"6. Increase temperature 0.7→0.4".
    temperature: float = 0.5,
    history: list[dict] | None = None,
    model_override: str | None = None,
) -> str:
    """
    Call hosted Gemma (or any Generative Language API model) via REST.

    - systemInstruction carries the system prompt + context so the model treats
      it as directives, not as content to summarise.
    - history (optional): list of prior turn dicts in API format
      [{"role":"user","parts":[...]}, {"role":"model","parts":[...]}]
      placed before the current user message for multi-turn context.
    - model_override: use a different model for this call (e.g. smaller intent model).
    - Thought parts (thought=True) are filtered out — gemma-4 emits these as
      internal reasoning that must not be shown to end users.
    """
    model_name = (model_override or _GEMINI_MODEL).removeprefix("models/")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={_GEMINI_API_KEY}"
    )
    # Build the contents array: history turns (if any) + current user message
    contents: list[dict] = list(history or [])
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            candidate = data["candidates"][0]
            finish_reason = candidate.get("finishReason", "UNKNOWN")
            if finish_reason not in ("STOP", "MAX_TOKENS"):
                logger.warning("Gemma unexpected finishReason=%s", finish_reason)
            # Filter out thought parts (thinking/reasoning — gemma-4 returns these
            # as parts with thought=True which must not be shown to the user).
            text = "".join(
                p["text"] for p in candidate["content"]["parts"]
                if not p.get("thought", False)
            ).strip()
            usage = data.get("usageMetadata", {})
            logger.info(
                "Gemma response | model=%s | finish=%s | output_tokens=%d | chars=%d",
                model_name,
                finish_reason,
                usage.get("candidatesTokenCount", 0),
                len(text),
            )
            return text
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Gemma HTTP error | status=%s | body=%s",
            exc.response.status_code, exc.response.text[:300],
        )
        raise
    except Exception:
        logger.exception("Gemma call failed")
        raise


def _strip_json_fences(text: str) -> str:
    """
    Remove Markdown code fences that Gemma sometimes wraps around JSON output
    even when instructed not to.  Handles both ```json ... ``` and ``` ... ```.
    """
    import re
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _rag_fallback_response(message: str, rag_chunks: list) -> str:
    """
    User-friendly fallback when Gemini is temporarily unavailable after retries.
    Uses already-retrieved RAG chunks to give a useful answer without the AI model.
    Never exposes internal terms (RAG, ChromaDB, chunks) to the end user.
    """
    relevant = [c for c in rag_chunks if c.get("distance", 1.0) < 0.7]

    if relevant:
        lines = [
            "Halo! Saya BIMA-AI. Layanan AI utama sedang mengalami gangguan sementara, "
            "tapi saya sudah menemukan informasi regulasi OSS yang relevan untuk pertanyaan Anda:\n"
        ]
        for chunk in relevant[:3]:
            kbli = chunk.get("kbli_code", "")
            section = chunk.get("section", "")
            skala = chunk.get("skala", "")
            content = chunk["content"][:700].strip()

            label_parts = []
            if kbli:
                label_parts.append(f"KBLI {kbli}")
            if section:
                label_parts.append(section.replace("_", " ").title())
            if skala:
                label_parts.append(skala)
            label = " · ".join(label_parts) if label_parts else "Regulasi OSS"

            lines.append(f"📋 *{label}*\n{content}\n")

        lines.append(
            "_⚠️ Jawaban ini diambil langsung dari basis data regulasi tanpa analisis AI penuh. "
            "Silakan coba lagi dalam beberapa menit untuk jawaban yang lebih lengkap._\n"
        )
        lines.append(f"[Buka Portal BIMA-AI →]({_PORTAL_URL})")
        return "\n".join(lines)

    # No relevant RAG context — give a helpful generic response
    return (
        "Halo! Saya BIMA-AI, asisten perizinan usaha DPMPTSP Jawa Tengah. 🙏\n\n"
        "Layanan AI sedang mengalami gangguan teknis sementara. "
        "Untuk bantuan segera:\n"
        "1. Coba kirim pertanyaan Anda lagi dalam beberapa menit\n"
        "2. Kunjungi portal BIMA-AI untuk panduan langkah demi langkah\n"
        "3. Hubungi petugas DPMPTSP Jawa Tengah untuk kasus kompleks\n\n"
        f"[Buka Portal BIMA-AI →]({_PORTAL_URL})"
    )


def _smart_placeholder(message: str, user_ctx: dict, rag_chunks: list) -> str:
    """Structured placeholder when Gemini API key is missing (dev/demo mode)."""
    has_rag = bool(rag_chunks)
    rag_note = (
        f"\n\n📚 Ditemukan {len(rag_chunks)} referensi regulasi OSS yang relevan."
        if has_rag
        else "\n\n⚠️ Basis data regulasi OSS belum tersedia."
    )
    portal = f"\n\n[Buka Portal BIMA-AI →]({_PORTAL_URL})"
    return (
        f"[Demo Mode — konfigurasi server belum selesai]\n\n"
        f"Pertanyaan Anda: *{message[:100]}*\n\n"
        f"Saya siap membantu proses perizinan OSS RBA Anda."
        f"{rag_note}{portal}"
    )


# ---------------------------------------------------------------------------
# 2. Backend logger (fire-and-forget)
# ---------------------------------------------------------------------------

async def log_to_backend(
    user_id: str, prompt: str, ai_response: str, channel: str = "telegram"
) -> None:
    if not _LARAVEL_API_KEY:
        logger.warning("LARAVEL_API_KEY not set — skipping log | user_id=%s", user_id)
        return

    session_id = f"{channel[:2]}-{user_id}-{uuid.uuid4().hex[:8]}"
    await _post_log(session_id, 0, channel, "user_message", prompt, user_id)
    await _post_log(session_id, 1, channel, "ai_response", ai_response, user_id)


async def _post_log(
    session_id: str,
    turn_index: int,
    channel: str,
    message_type: str,
    content: str,
    user_id: str,
) -> None:
    url = f"{_LARAVEL_BACKEND_URL}/api/internal/ai-logs"
    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "X-Internal-Key": _LARAVEL_API_KEY,
    }
    payload: dict[str, Any] = {
        "session_id":   session_id,
        "turn_index":   turn_index,
        "channel":      channel,
        "message_type": message_type,
        "content":      content,
    }
    if user_id.isdigit():
        payload["user_id"] = int(user_id)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if not resp.is_success:
                logger.warning(
                    "Log POST failed | status=%s | body=%s",
                    resp.status_code, resp.text[:200],
                )
    except Exception:
        logger.exception("log_to_backend failed | user_id=%s", user_id)


