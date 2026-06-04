"""
Lightweight, reusable conversational-intent classifier for the guided
submission flow — the redesign that lets a citizen reply naturally instead of
typing rigid keywords (SIAP / YA / BATAL / SELESAI).

WHAT THIS REPLACES
  The old flow gated each transition on an exact keyword:
    * CONFIRMING_START → an explicit "SIAP" / "MULAI"
    * REVIEW          → an explicit "YA" to submit, "BATAL" to cancel
    * COLLECTING_DOCS → an explicit "SELESAI" to score
  The product owner wants BIMA to UNDERSTAND the citizen's intent in warm,
  natural Bahasa Indonesia. This module is the single seam that does that: a
  strict-JSON LLM call that maps a free-text reply at the CONFIRM stage to one
  of four intents.

REUSE, NOT REINVENT
  The Gemini call reuses `ai_handler._call_gemma` + `ai_handler._strip_json_fences`
  verbatim — same REST client, same lightweight intent model selection, same
  "strip ```json fences then json.loads" parsing as `analyze_user_intent` and
  `license_resolver`. Temperature 0.0 for a deterministic mapping; JSON enforced
  by prompt only (the hosted Gemma has no response_mime_type — see CLAUDE.md).

RESILIENCE
  NEVER raises. When the LLM is down / unparseable, we fall back to a
  conservative keyword classifier so a clear "ya" / "tidak" still works and the
  flow never hard-blocks. The literal word "batal" is always treated as a strong
  DECLINE before the LLM is even consulted.

INTENTS (CONFIRM stage)
  AFFIRM   — the citizen agrees to submit now ("ya", "oke pak lanjut", "yakin",
             "boleh", "setuju", "betul", "gas", "silakan").
  DECLINE  — the citizen wants to hold/cancel ("tidak", "batal", "nanti dulu",
             "belum", "jangan dulu").
  CORRECT  — the citizen is fixing a field ("NIK saya yang benar 33..", "nama
             usaha PT Maju Jaya", "namanya Budi bukan Budy").
  QUESTION — the citizen is asking something ("ini buat apa?", "berapa lama?").
  UNCLEAR  — none of the above with confidence → ask a gentle clarifier.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("bima_ai.submission_intent")

# Lightweight model for the JSON-only call — same precedence the other JSON
# pre-calls (analyze_user_intent, license_resolver) use: intent model first,
# then the main model.
_INTENT_MODEL: str = (
    os.getenv("GEMINI_INTENT_MODEL", "")
    or os.getenv("GEMINI_MODEL", "")
    or "models/gemma-3-27b-it"
)

_MAX_TOKENS = 80

# Canonical intent vocabulary.
AFFIRM = "AFFIRM"
DECLINE = "DECLINE"
CORRECT = "CORRECT"
QUESTION = "QUESTION"
UNCLEAR = "UNCLEAR"

_VALID = {AFFIRM, DECLINE, CORRECT, QUESTION, UNCLEAR}


# ---------------------------------------------------------------------------
# Keyword fallback — used when the LLM is unavailable so the flow never
# hard-blocks on a clear yes / no. Conservative by construction: only obvious
# words map; anything ambiguous returns UNCLEAR so we ask a gentle clarifier.
# ---------------------------------------------------------------------------

# Decline cues — matched if they LEAD the reply (so "tidak dulu ya", "nanti
# saja deh" classify cleanly). The literal "batal" is the strong cancel.
_DECLINE_RE = re.compile(
    r"^\s*(tidak|ga+k?|engga+k?|nggak|jangan|batal(?:kan)?|cancel|"
    r"stop|berhenti|belum|nanti|tunda|skip|ga\s+jadi|gak\s+jadi|tidak\s+jadi)\b",
    re.IGNORECASE,
)

# Affirm cues — matched if they LEAD the reply, so common warm yeses like
# "ya saya yakin", "oke pak lanjut", "iya setuju", "boleh, lanjutkan" all map to
# AFFIRM in the LLM-down fallback. Conservative: a reply that LEADS with a
# decline cue is handled first (see _keyword_intent ordering).
_AFFIRM_RE = re.compile(
    r"^\s*(ya|iya|iy|yo?i|yes|y|ok|oke|okay|okey|sip|setuju|"
    r"lanjut(?:kan)?|benar|betul|bener|yakin|gas|gaskeun|jalankan|kirim|"
    r"submit|boleh|silakan|silahkan|ayo|ayok|deal|cus|baik|mantap|"
    r"lakukan|proses)\b",
    re.IGNORECASE,
)

# A literal "batal" check exposed for callers that want a hard cancel signal
# independent of the LLM (the flow uses this so the word always cancels).
_BATAL_WORD_RE = re.compile(
    r"^\s*(batal|batalkan|cancel|stop|berhenti)\s*[.!]*\s*$", re.IGNORECASE
)


def is_hard_cancel(message: str) -> bool:
    """True for the literal strong-cancel words ('batal', 'cancel', 'stop').

    The product keeps 'batal' as a strong decline that works regardless of the
    LLM. Callers can short-circuit on this before any model call.
    """
    return bool(_BATAL_WORD_RE.match((message or "").strip()))


def _keyword_intent(message: str) -> str:
    """Conservative keyword classifier — the LLM-down fallback.

    Order matters: DECLINE before AFFIRM so 'tidak jadi' isn't swallowed by a
    loose affirm token. Returns UNCLEAR for anything that isn't an obvious
    yes/no — the flow then asks a gentle clarifier rather than guessing.
    """
    msg = (message or "").strip()
    if not msg:
        return UNCLEAR
    if _DECLINE_RE.match(msg):
        return DECLINE
    if _AFFIRM_RE.match(msg):
        return AFFIRM
    return UNCLEAR


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a JSON-only API. Output valid JSON and nothing else — no Markdown, "
    "no code fences, no commentary.\n"
    "\n"
    "A citizen is filing a licence application via WhatsApp with the assistant "
    "BIMA. BIMA has just read the citizen's uploaded documents, shown a summary "
    "(applicant name, masked NIK, business name) and a readiness score, and "
    "asked: 'Apakah Bapak/Ibu yakin untuk mengajukan izin ini sekarang?'.\n"
    "\n"
    "Classify the citizen's reply into exactly ONE intent:\n"
    "  AFFIRM   - agrees to submit NOW (e.g. 'ya', 'iya yakin', 'oke pak "
    "lanjut', 'boleh', 'setuju', 'betul', 'gas', 'silakan kirim', 'lanjutkan').\n"
    "  DECLINE  - wants to hold, cancel, or do it later (e.g. 'tidak', 'belum', "
    "'nanti dulu', 'jangan dulu', 'batal', 'ga jadi').\n"
    "  CORRECT  - is FIXING a piece of their data, not agreeing (e.g. 'NIK saya "
    "yang benar 3374...', 'nama usaha PT Maju Jaya', 'namanya Budi bukan Budy', "
    "'ralat alamat usaha ...'). Choose this whenever the reply supplies a "
    "corrected name / NIK / business name value.\n"
    "  QUESTION - is asking something before deciding (e.g. 'ini untuk apa?', "
    "'berapa lama prosesnya?', 'aman tidak datanya?').\n"
    "  UNCLEAR  - none of the above, or genuinely ambiguous.\n"
    "\n"
    "If the reply both asks AND corrects, prefer CORRECT. If it both asks AND "
    "affirms, prefer AFFIRM.\n"
    "\n"
    "Exact schema:\n"
    '{"intent": "AFFIRM" | "DECLINE" | "CORRECT" | "QUESTION" | "UNCLEAR"}'
)


async def classify_confirm_intent(message: str) -> str:
    """Classify a citizen's CONFIRM-stage reply into one of the intents.

    Strategy:
      1. The literal strong-cancel word ('batal') is ALWAYS DECLINE — no LLM.
      2. Otherwise ask the lightweight Gemma JSON classifier.
      3. On any LLM error / unparseable output → conservative keyword fallback
         (a clear 'ya'/'tidak' still works; everything else → UNCLEAR).

    NEVER raises — returns one of the canonical intent strings.
    """
    msg = (message or "").strip()
    if not msg:
        return UNCLEAR

    # The owner kept 'batal' as a strong decline that must work regardless of
    # the model's availability or mood.
    if is_hard_cancel(msg):
        return DECLINE

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return _keyword_intent(msg)

    try:
        from services.ai_handler import _call_gemma, _strip_json_fences

        raw = await _call_gemma(
            _SYSTEM,
            msg,
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            model_override=_INTENT_MODEL,
        )
        parsed = json.loads(_strip_json_fences(raw))
        intent = str(parsed.get("intent", "")).strip().upper()
        if intent in _VALID:
            logger.info("confirm-intent classified | intent=%s | msg_len=%d", intent, len(msg))
            return intent
        # Model drifted to an out-of-vocab label → fall back.
        logger.info(
            "confirm-intent out-of-vocab '%s' — keyword fallback | msg_len=%d",
            intent, len(msg),
        )
        return _keyword_intent(msg)
    except Exception:
        logger.warning(
            "confirm-intent LLM failed — keyword fallback | msg_len=%d", len(msg)
        )
        return _keyword_intent(msg)
