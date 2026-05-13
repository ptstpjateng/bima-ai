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

_PORTAL_URL = "https://project-5z22k.vercel.app"

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

        # A1 — KBLI-prefixed RAG query for tighter results when a code is known
        rag_query = f"KBLI {detected_kbli} {message}" if detected_kbli else message
        n_results = 8 if detected_kbli else 4

        logger.info(
            "Intent | phase=%s kbli=%s scale=%s | rag_query=%r | n_results=%d | user_id=%s",
            intent.get("phase"), detected_kbli, intent.get("detected_scale"),
            rag_query, n_results, user_id,
        )

        rag_chunks = await asyncio.get_event_loop().run_in_executor(
            None, lambda: query_regulations(rag_query, n_results=n_results)
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

        # A3 — pass last 2 conversation turns so follow-up questions have context
        history = _get_history(user_id)

        if _GEMINI_API_KEY:
            try:
                ai_response = await _call_gemma_with_retry(
                    system_instruction, message, history=history
                )
            except Exception:
                logger.warning(
                    "Gemma unavailable after retries — using RAG-based fallback | user_id=%s",
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
        raw = await _call_gemma(
            _INTENT_SYSTEM, message,
            max_tokens=128, temperature=0.0,
            model_override=_GEMINI_INTENT_MODEL,
        )
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


async def _call_gemma(
    system_prompt: str,
    user_message: str,
    *,
    max_tokens: int = 2048,
    temperature: float = 0.7,
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


