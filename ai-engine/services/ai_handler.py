"""
BIMA-AI – AI Handler Service

Full pipeline for every inbound message:
  1. fetch_user_context  – pull user's business profile + license vault from Laravel
  2. query_regulations   – semantic RAG search in ChromaDB
  3. generate_ai_response – call Gemini LLM (falls back to smart placeholder)
  4. send_platform_reply – send reply to WhatsApp or Telegram
  5. log_to_backend      – async POST to Laravel /api/ai-logs (fire-and-forget)

Persona & lifecycle model: see BIMA_PERSONA.md in the project root.
"""

import logging
import os
import uuid
from typing import Any, Literal

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
_GEMINI_MODEL: str        = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_WHATSAPP_API_TOKEN: str   = os.getenv("WHATSAPP_API_TOKEN", "")
_WHATSAPP_API_VERSION: str = os.getenv("WHATSAPP_API_VERSION", "v19.0")
_TELEGRAM_BOT_TOKEN: str   = os.getenv("TELEGRAM_BOT_TOKEN", "")

_PORTAL_URL = "https://project-5z22k.vercel.app"

Platform = Literal["whatsapp", "telegram"]

# ---------------------------------------------------------------------------
# System prompt  (BIMA_PERSONA.md → Phase 1 / 2 / 3)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
Kamu adalah BIMA-AI, asisten cerdas milik DPMPTSP Jawa Tengah yang membantu
pelaku UMKM Indonesia menavigasi sistem perizinan OSS RBA (Online Single
Submission – Risk-Based Approach).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ATURAN DASAR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Bersikap membantu, ringkas, dan empatik. Terjemahkan bahasa birokrasi menjadi
  panduan praktis yang langsung bisa dilakukan pemilik usaha kecil.
• Jangan mengarang nomor pasal, biaya, atau referensi hukum. Kalau tidak pasti,
  katakan "mohon konfirmasi ke DPMPTSP setempat".
• Balas dalam Bahasa Indonesia jika pengguna menulis Bahasa Indonesia, dalam
  Bahasa Inggris jika Bahasa Inggris.
• Jawaban singkat — muat di layar HP (≤ 5 paragraf pendek atau daftar singkat).
• Jika ada konteks profil pengguna, gunakan info tersebut (nama usaha, KBLI, skala).
• Jika ada konteks regulasi dari ChromaDB, gunakan sebagai referensi akurat dan
  prioritaskan di atas pengetahuan umummu.

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
  "[Buka Portal BIMA-AI →](https://project-5z22k.vercel.app)"
  (Untuk Telegram: kirim sebagai inline button jika memungkinkan)
• Jika pengguna mengalami hambatan (dokumen kurang, field membingungkan),
  berikan solusi spesifik atau arahkan ke petugas DPMPTSP Jawa Tengah.

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

Basis data regulasi OSS spesifik BIMA-AI saat ini belum tersedia karena pipeline
pengambilan data masih berjalan. Jawab menggunakan pengetahuan umum OSS RBA dan
perizinan usaha Indonesia, lalu TAMBAHKAN catatan ini di akhir (persis):

"ℹ️ Basis data regulasi OSS spesifik BIMA-AI sedang dalam proses pembaruan.
Jawaban ini menggunakan basis pengetahuan AI umum."
""".strip()


# ---------------------------------------------------------------------------
# 1. LLM call (Gemini) with graceful fallback
# ---------------------------------------------------------------------------

async def generate_ai_response(user_id: str, message: str) -> str:
    """
    Generate a personalized AI reply:
    1. Fetch user context from Laravel (business profile + licenses)
    2. Retrieve relevant regulations from ChromaDB (RAG)
    3. Call Gemini — falls back to structured placeholder if no key
    """
    from services.rag_service import format_rag_context, query_regulations
    from services.user_context import fetch_user_context, format_user_context

    try:
        import asyncio
        user_ctx_task = asyncio.create_task(fetch_user_context(user_id))
        rag_chunks    = await asyncio.get_event_loop().run_in_executor(
            None, query_regulations, message
        )
        user_ctx = await user_ctx_task

        user_ctx_str = format_user_context(user_ctx)
        rag_ctx_str  = format_rag_context(rag_chunks)
        has_rag      = bool(rag_ctx_str)

        if not has_rag:
            logger.info(
                "RAG returned 0 usable chunks — activating fallback prompt | user_id=%s", user_id
            )

        system = _SYSTEM_PROMPT if has_rag else f"{_SYSTEM_PROMPT}\n\n{_FALLBACK_SYSTEM_ADDITION}"
        parts = [system]
        if user_ctx_str:
            parts.append("\n" + user_ctx_str)
        if rag_ctx_str:
            parts.append("\n" + rag_ctx_str)
        parts.append(f"\nPertanyaan pengguna: {message}")
        full_prompt = "\n".join(parts)

        if _GEMINI_API_KEY:
            return await _call_gemini(full_prompt)
        else:
            return _smart_placeholder(message, user_ctx, rag_chunks)

    except Exception:
        logger.exception("generate_ai_response failed | user_id=%s", user_id)
        return (
            "Maaf, terjadi gangguan teknis sementara. "
            "Silakan coba lagi dalam beberapa saat. Terima kasih atas kesabaran Anda."
        )


async def _call_gemini(prompt: str) -> str:
    """Call Gemini via REST API."""
    model_name = _GEMINI_MODEL.removeprefix("models/")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={_GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1024,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return (
                data["candidates"][0]["content"]["parts"][0]["text"]
                .strip()
            )
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Gemini HTTP error | status=%s | body=%s",
            exc.response.status_code, exc.response.text[:300],
        )
        raise
    except Exception:
        logger.exception("Gemini call failed")
        raise


def _smart_placeholder(message: str, user_ctx: dict, rag_chunks: list) -> str:
    """Structured placeholder when Gemini API key is missing."""
    has_rag = bool(rag_chunks)
    rag_note = (
        f"\n\n📚 *Konteks RAG ditemukan:* {len(rag_chunks)} chunk regulasi relevan."
        if has_rag
        else "\n\n⚠️ *RAG:* Belum ada data regulasi di ChromaDB."
    )
    portal = f"\n\n[Buka Portal BIMA-AI →]({_PORTAL_URL})"
    return (
        f"[BIMA-AI Demo — GEMINI_API_KEY belum dikonfigurasi]\n\n"
        f"Pesan Anda: *{message[:100]}*\n\n"
        f"Saya siap membantu proses perizinan OSS RBA Anda."
        f"{rag_note}{portal}"
    )


# ---------------------------------------------------------------------------
# 2. Platform reply dispatchers
# ---------------------------------------------------------------------------

async def _send_whatsapp_reply(
    phone_number_id: str, recipient_wa_id: str, message: str
) -> None:
    url = (
        f"https://graph.facebook.com/{_WHATSAPP_API_VERSION}"
        f"/{phone_number_id}/messages"
    )
    headers = {
        "Authorization": f"Bearer {_WHATSAPP_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_wa_id,
        "type": "text",
        "text": {"body": message[:4096]},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info("WhatsApp reply sent | wa_id=%s", recipient_wa_id)
    except Exception:
        logger.exception("WhatsApp reply failed | wa_id=%s", recipient_wa_id)


async def _send_telegram_reply(chat_id: str | int, message: str) -> None:
    url = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"

    # If response contains Execution-phase portal link → add Telegram inline button
    has_portal_link = _PORTAL_URL in message
    payload: dict[str, Any] = {
        "chat_id":    chat_id,
        "text":       message[:4096],
        "parse_mode": "Markdown",
    }
    if has_portal_link:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "🚀 Buka Portal BIMA-AI", "url": _PORTAL_URL}
            ]]
        }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info("Telegram reply sent | chat_id=%s", chat_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            payload.pop("parse_mode", None)
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(url, json=payload)
            except Exception:
                logger.exception("Telegram plain reply failed | chat_id=%s", chat_id)
        else:
            logger.warning(
                "Telegram API error | chat_id=%s | status=%s",
                chat_id, exc.response.status_code,
            )
    except Exception:
        logger.exception("Telegram reply failed | chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# 3. Backend logger (fire-and-forget)
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


# ---------------------------------------------------------------------------
# 4. Main pipeline entry point
# ---------------------------------------------------------------------------

async def process_message(
    user_id: str,
    message: str,
    platform: Platform,
    reply_context: dict[str, Any],
) -> None:
    logger.info("Pipeline start | platform=%s | user_id=%s", platform, user_id)

    ai_response = await generate_ai_response(user_id, message)

    try:
        if platform == "whatsapp":
            await _send_whatsapp_reply(
                phone_number_id=reply_context["phone_number_id"],
                recipient_wa_id=reply_context["recipient_wa_id"],
                message=ai_response,
            )
        elif platform == "telegram":
            await _send_telegram_reply(chat_id=reply_context["chat_id"], message=ai_response)
    except Exception:
        logger.exception("Reply dispatch failed | platform=%s | user_id=%s", platform, user_id)

    await log_to_backend(user_id, message, ai_response)
    logger.info("Pipeline complete | platform=%s | user_id=%s", platform, user_id)
