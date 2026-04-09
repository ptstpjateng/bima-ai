"""
BIMA-AI – AI Handler Service

Full pipeline for every inbound message:
  1. analyze_user_intent – lightweight JSON call to classify phase (1/2/3) + extract KBLI
  2. fetch_user_context  – pull user's business profile + license vault from Laravel
  3. query_regulations   – semantic RAG search in ChromaDB (KBLI-targeted when detected)
  4. generate_ai_response – call hosted Gemma LLM (falls back to RAG-based response)
  5. send_platform_reply – send reply to WhatsApp or Telegram
  6. log_to_backend      – async POST to Laravel /api/ai-logs (fire-and-forget)

Primary LLM: gemma-3-27b-it via Google Generative Language API (open-weights, no VPS GPU).
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
# Primary model: gemma-3-27b-it (open-weights hosted on Google AI Studio infra).
# Override via GEMINI_MODEL env var — e.g. models/gemma-3-12b-it for lighter load.
_GEMINI_MODEL: str        = os.getenv("GEMINI_MODEL", "models/gemma-3-27b-it")

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
# 1. Intent classification + LLM call (hosted Gemma) with graceful fallback
# ---------------------------------------------------------------------------

async def generate_ai_response(user_id: str, message: str) -> str:
    """
    Generate a personalized AI reply:
    1. analyze_user_intent — classify phase + extract KBLI code (lightweight JSON call)
    2. Fetch user context from Laravel (business profile + licenses)
    3. Retrieve relevant regulations from ChromaDB (KBLI-targeted RAG)
    4. Call Gemma — falls back to RAG-based response if API unavailable
    """
    from services.rag_service import format_rag_context, query_regulations
    from services.user_context import fetch_user_context, format_user_context

    try:
        import asyncio

        # Step 1 — lightweight intent classification (runs concurrently with user context fetch)
        intent_task   = asyncio.create_task(analyze_user_intent(message))
        user_ctx_task = asyncio.create_task(fetch_user_context(user_id))

        intent   = await intent_task
        user_ctx = await user_ctx_task

        # Use detected KBLI to run a tighter RAG query (more results for known KBLI)
        detected_kbli = intent.get("kbli_code")
        rag_query     = f"KBLI {detected_kbli} {message}" if detected_kbli else message
        n_results     = 8 if detected_kbli else 4

        rag_chunks = await asyncio.get_event_loop().run_in_executor(
            None, lambda: query_regulations(rag_query, n_results=n_results)
        )

        user_ctx_str = format_user_context(user_ctx)
        rag_ctx_str  = format_rag_context(rag_chunks)
        has_rag      = bool(rag_ctx_str)

        logger.info(
            "Intent | phase=%s kbli=%s scale=%s | rag_chunks=%d | user_id=%s",
            intent.get("phase"), detected_kbli, intent.get("detected_scale"),
            len(rag_chunks), user_id,
        )
        if not has_rag:
            logger.info(
                "RAG returned 0 usable chunks — activating fallback prompt | user_id=%s", user_id
            )

        # Build system instruction: base prompt + user profile + RAG context
        system = _SYSTEM_PROMPT if has_rag else f"{_SYSTEM_PROMPT}\n\n{_FALLBACK_SYSTEM_ADDITION}"
        system_parts = [system]
        if user_ctx_str:
            system_parts.append(user_ctx_str)
        if rag_ctx_str:
            system_parts.append(rag_ctx_str)
        system_instruction = "\n\n".join(system_parts)

        if _GEMINI_API_KEY:
            try:
                return await _call_gemma_with_retry(system_instruction, message)
            except Exception:
                logger.warning(
                    "Gemma unavailable after retries — using RAG-based fallback | user_id=%s",
                    user_id,
                )
                return _rag_fallback_response(message, rag_chunks)
        else:
            return _smart_placeholder(message, user_ctx, rag_chunks)

    except Exception:
        logger.exception("generate_ai_response failed | user_id=%s", user_id)
        return (
            "Maaf, terjadi gangguan teknis sementara. "
            "Silakan coba lagi dalam beberapa saat. Terima kasih atas kesabaran Anda."
        )


_INTENT_SCHEMA = """{
  "phase": <integer 1, 2, or 3>,
  "kbli_code": <string like "56102" or null if not mentioned>,
  "detected_scale": <string like "Mikro", "Kecil", "Menengah", "Besar" or null>
}"""

_INTENT_SYSTEM = (
    "You are a JSON-only API. You must output valid JSON and absolutely nothing else. "
    "Do not use Markdown code blocks, backticks, or any surrounding text. "
    "Classify the user message into one of three business licensing phases:\n"
    "  Phase 1 (Pra-Perizinan): exploring, planning, asking what permits are needed\n"
    "  Phase 2 (Eksekusi): ready to apply, asking for step-by-step, mentions a KBLI code\n"
    "  Phase 3 (Pasca-Perizinan): already has permit, asking about obligations or growth\n"
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
        return {"phase": 1, "kbli_code": None, "detected_scale": None}

    try:
        raw = await _call_gemma(_INTENT_SYSTEM, message, max_tokens=128, temperature=0.0)
        cleaned = _strip_json_fences(raw)
        intent = json.loads(cleaned)
        # Normalise — ensure required keys exist
        return {
            "phase":          int(intent.get("phase", 1)),
            "kbli_code":      intent.get("kbli_code") or None,
            "detected_scale": intent.get("detected_scale") or None,
        }
    except Exception:
        logger.warning("analyze_user_intent failed — using defaults | msg_len=%d", len(message))
        return {"phase": 1, "kbli_code": None, "detected_scale": None}


async def _call_gemma_with_retry(
    system_prompt: str, user_message: str, max_attempts: int = 3, **kwargs
) -> str:
    """Call Gemma with exponential backoff for transient 429/503 errors.

    Raises the final exception if all attempts fail so the caller can fall back
    to the RAG-based response.
    """
    import asyncio

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await _call_gemma(system_prompt, user_message, **kwargs)
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


async def _call_gemma(
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    """
    Call hosted Gemma (or any model on the Generative Language API) via REST.

    Uses systemInstruction for the system prompt so the model treats it as
    directives, not as content to summarise or continue.  The user_message
    goes in contents[].  This fixes the "Gemma echoes the system prompt"
    regression caused by passing everything as a single flat text blob.

    Notable differences from the old Gemini call:
    - No thinkingConfig (Gemma models don't support it)
    - Response text may contain Markdown code fences around JSON; callers that
      need JSON should use _strip_json_fences() before parsing.
    """
    model_name = _GEMINI_MODEL.removeprefix("models/")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={_GEMINI_API_KEY}"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
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
