"""
BIMA-AI – AI Handler Service

Full pipeline for every inbound message:
  1. fetch_user_context  – pull user's business profile + license vault from Laravel
  2. query_regulations   – semantic RAG search in ChromaDB
  3. generate_ai_response – call Gemini LLM (falls back to smart placeholder)
  4. send_platform_reply – send reply to WhatsApp or Telegram
  5. log_to_backend      – async POST to Laravel /api/ai-logs (fire-and-forget)
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
_GEMINI_MODEL: str        = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

_WHATSAPP_API_TOKEN: str   = os.getenv("WHATSAPP_API_TOKEN", "")
_WHATSAPP_API_VERSION: str = os.getenv("WHATSAPP_API_VERSION", "v19.0")
_TELEGRAM_BOT_TOKEN: str   = os.getenv("TELEGRAM_BOT_TOKEN", "")

Platform = Literal["whatsapp", "telegram"]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
Kamu adalah BIMA-AI, asisten cerdas yang membantu pelaku UMKM Indonesia
menavigasi sistem perizinan OSS RBA (Online Single Submission – Risk-Based
Approach) yang dikelola oleh DPMPTSP Jawa Tengah.

Aturan utama:
- Bersikap membantu, ringkas, dan empatik. Terjemahkan bahasa birokrasi menjadi
  panduan praktis yang langsung bisa dilakukan oleh pemilik usaha kecil.
- Spesialisasi perizinan Jawa Tengah: persyaratan DPMPTSP, kode KBLI, regulasi
  daerah, dan kebijakan Pemprov Jateng.
- Jika pengguna mendeskripsikan kegiatan usaha, identifikasi kode KBLI yang tepat
  dan jelaskan izin OSS RBA, NIB, atau sertifikat yang dibutuhkan.
- Jika ada konteks profil pengguna, gunakan informasi tersebut untuk memberikan
  saran yang dipersonalisasi (sebutkan nama usaha, KBLI, skala, dll).
- Jika ada konteks regulasi (dari ChromaDB), gunakan sebagai referensi akurat.
  Jangan mengarang nomor pasal atau referensi hukum.
- Ajukan satu pertanyaan klarifikasi jika permintaan ambigu.
- Balas dalam Bahasa Indonesia jika pengguna menulis dalam Bahasa Indonesia,
  dalam Bahasa Inggris jika dalam Bahasa Inggris.
- Jawaban singkat — muat dalam layar HP (≤ 5 paragraf pendek atau daftar singkat).
""".strip()


# ---------------------------------------------------------------------------
# 1. LLM call (Gemini 1.5 Flash) with graceful fallback
# ---------------------------------------------------------------------------

async def generate_ai_response(user_id: str, message: str) -> str:
    """
    Generate a personalized AI reply:
    1. Fetch user context from Laravel (business profile + licenses)
    2. Retrieve relevant regulations from ChromaDB (RAG)
    3. Call Gemini 1.5 Flash — falls back to structured placeholder if no key
    """
    from services.rag_service import format_rag_context, query_regulations
    from services.user_context import fetch_user_context, format_user_context

    try:
        # --- Step 1: Fetch user context and RAG in parallel ---
        import asyncio
        user_ctx_task = asyncio.create_task(fetch_user_context(user_id))
        rag_chunks    = await asyncio.get_event_loop().run_in_executor(
            None, query_regulations, message
        )
        user_ctx = await user_ctx_task

        user_ctx_str = format_user_context(user_ctx)
        rag_ctx_str  = format_rag_context(rag_chunks)

        # --- Step 2: Build full prompt ---
        parts = [_SYSTEM_PROMPT]
        if user_ctx_str:
            parts.append("\n" + user_ctx_str)
        if rag_ctx_str:
            parts.append("\n" + rag_ctx_str)
        parts.append(f"\nPertanyaan pengguna: {message}")
        full_prompt = "\n".join(parts)

        # --- Step 3: Call Gemini ---
        if _GEMINI_API_KEY:
            return await _call_gemini(full_prompt)
        else:
            # Smart placeholder that still uses context
            return _smart_placeholder(message, user_ctx, rag_chunks)

    except Exception:
        logger.exception("generate_ai_response failed | user_id=%s", user_id)
        return (
            "Maaf, terjadi gangguan teknis sementara. "
            "Silakan coba lagi dalam beberapa saat. Terima kasih atas kesabaran Anda."
        )


async def _call_gemini(prompt: str) -> str:
    """Call Gemini 1.5 Flash via REST API."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{_GEMINI_MODEL}:generateContent?key={_GEMINI_API_KEY}"
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
            text = (
                data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            if text:
                logger.info("Gemini response received | chars=%d", len(text))
                return text.strip()
    except Exception:
        logger.exception("Gemini API call failed — using placeholder.")

    return "Maaf, layanan AI sedang tidak tersedia. Silakan coba lagi sebentar."


def _smart_placeholder(
    message: str,
    user_ctx: dict,
    rag_chunks: list,
) -> str:
    """
    Contextual placeholder used when GEMINI_API_KEY is not set.
    Acknowledges user's business context and shows RAG is working.
    """
    user = user_ctx.get("user", {}) if user_ctx.get("found") else {}
    name = user.get("business_name") or user.get("name") or "Anda"

    rag_note = ""
    if rag_chunks:
        top = rag_chunks[0]
        rag_note = f"\n\nSaya menemukan referensi regulasi terkait: *{top.get('title', '')}*."

    return (
        f"Halo, *{name}*! Pertanyaan Anda tentang:\n\n"
        f"_{message}_\n\n"
        f"sedang saya proses. Integrasi LLM (Gemini) belum dikonfigurasi — "
        f"tambahkan `GEMINI_API_KEY` ke file `.env` mesin AI untuk mengaktifkan "
        f"jawaban cerdas penuh berbasis RAG.{rag_note}\n\n"
        f"Ada yang bisa saya bantu lainnya?"
    )


# ---------------------------------------------------------------------------
# 2. Platform reply senders
# ---------------------------------------------------------------------------

async def _send_whatsapp_reply(phone_number_id: str, recipient_wa_id: str, message: str) -> None:
    if not _WHATSAPP_API_TOKEN:
        logger.warning("WHATSAPP_API_TOKEN not set | recipient=%s", recipient_wa_id)
        return

    url = f"https://graph.facebook.com/{_WHATSAPP_API_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {_WHATSAPP_API_TOKEN}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_wa_id,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info("WhatsApp reply sent | recipient=%s", recipient_wa_id)
    except Exception:
        logger.exception("WhatsApp reply failed | recipient=%s", recipient_wa_id)


async def _send_telegram_reply(chat_id: int, message: str) -> None:
    if not _TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set | chat_id=%s", chat_id)
        return

    url = f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict[str, Any] = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info("Telegram reply sent | chat_id=%s", chat_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400:
            # Retry without Markdown on parse error
            payload.pop("parse_mode", None)
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(url, json=payload)
            except Exception:
                logger.exception("Telegram plain reply failed | chat_id=%s", chat_id)
        else:
            logger.warning("Telegram API error | chat_id=%s | status=%s", chat_id, exc.response.status_code)
    except Exception:
        logger.exception("Telegram reply failed | chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# 3. Backend logger (fire-and-forget)
# ---------------------------------------------------------------------------

async def log_to_backend(user_id: str, prompt: str, ai_response: str) -> None:
    if not _LARAVEL_API_KEY:
        logger.warning("LARAVEL_API_KEY not set — skipping log | user_id=%s", user_id)
        return

    session_id = f"tg-{user_id}-{uuid.uuid4().hex[:8]}"

    # Log user message
    await _post_log(session_id, 0, "telegram", "user_message", prompt, user_id)
    # Log AI response
    await _post_log(session_id, 1, "telegram", "ai_response", ai_response, user_id)


async def _post_log(
    session_id: str,
    turn_index: int,
    channel: str,
    message_type: str,
    content: str,
    user_id: str,
) -> None:
    url = f"{_LARAVEL_BACKEND_URL}/api/ai-logs"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {_LARAVEL_API_KEY}",
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
                logger.warning("Log POST failed | status=%s | body=%s", resp.status_code, resp.text[:200])
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
