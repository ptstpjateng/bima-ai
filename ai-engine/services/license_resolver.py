"""
LLM-driven, catalogue-grounded SIAP licence resolver.

WHY THIS EXISTS
  The guided-submission flow used to resolve "which licence does the citizen
  want?" with a brittle pipeline: regex-strip stopwords from the message, then
  `name ILIKE '%query%'` filtered to stereotype='LICENSE'. That breaks on
  acronyms ("PKPP"), synonyms, word-order, and typos — and stopwords that
  slipped through the regex polluted the substring so nothing matched. A
  particularly bad case: a re-query that prepended "ajukan izin " turned "PKPP"
  into "ajukan izin PKPP" → zero ILIKE hits.

  This module replaces that with the directive behaviour: BIMA must UNDERSTAND
  THE INTENT and be GROUNDED IN THE WHOLE SIAP LICENCE CATALOGUE. We hand the
  COMPLETE catalogue (~384 `id | name | sektor` rows) to Gemini and let the
  model map the citizen's free-text request to the best licence id(s). We then
  VALIDATE every id the model returns against the catalogue (dropping any
  hallucination) and enrich it back to {license_id, name, sektor}.

REUSE, NOT REINVENT
  * The Gemini call reuses `ai_handler._call_gemma` + `ai_handler._strip_json_fences`
    verbatim — same REST client, same model selection (GEMINI_MODEL via
    model_override; defaults to the lightweight intent model), same
    "strip ```json fences then json.loads" parsing as `analyze_user_intent`.
    Temperature 0.0 for a deterministic mapping; JSON enforced by prompt only
    (the project's hosted Gemma has no response_mime_type — see CLAUDE.md).
  * The catalogue comes from `siap_tools.get_license_catalogue` — an in-memory
    TTL cache (officer-directory pattern). The static catalogue block leads the
    Gemini prompt so implicit prompt caching keys on it. (There is no explicit
    Gemini prompt-cache helper in this codebase — ai_handler only does implicit
    stable-prefix caching — so a plain call is used; the catalogue is small.)

OUTPUT SHAPE — identical to `siap_tools.siap_lookup_license`
  {"found": bool,
   "matches": [{"license_id": int, "name": str, "sektor": str|None}, ...],
   "note": str|None}
  so `guided_submission._start_session` consumes it unchanged: len(matches) > 1
  → numbered shortlist; == 1 → lock; found=False → "sebutkan nama izin" prompt.

FALLBACK
  If the catalogue is empty (SIAP DB down) OR the Gemini call errors / times
  out / returns unparseable, we fall back to a fuzzy parameterised SQL over the
  catalogue: tokenize the message, drop Indonesian filler stopwords, keep
  tokens len>=3, AND-match each token as an ILIKE substring; if that yields
  nothing, retry the same tokens OR'd. Same output shape. We log which path won
  (llm / fuzzy / fuzzy-on-empty-catalogue) without ever logging the raw
  citizen message (PII: it may carry a business name).

NEVER raises out of `resolve_license_intent` — every failure degrades to a
structured result the caller can render.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger("bima_ai.license_resolver")

# Lightweight model for the JSON-only mapping call. Defaults to the intent
# model (a smaller/faster variant on the free tier) and finally GEMINI_MODEL —
# the same precedence analyze_user_intent uses.
_RESOLVER_MODEL: str = (
    os.getenv("GEMINI_INTENT_MODEL", "")
    or os.getenv("GEMINI_MODEL", "")
    or "models/gemini-2.5-flash"  # gemma-3-27b-it was retired by Google (404)
)

# Cap the per-call generation — the model returns a tiny JSON object.
_RESOLVER_MAX_TOKENS = 256

# How many alternatives we keep beyond the best match (the shortlist the
# citizen picks from). _start_session shows up to 8; keep parity.
_MAX_ALTERNATIVES = 7

# ---------------------------------------------------------------------------
# System instruction — the model is a JSON-only licence mapper grounded in the
# catalogue we provide. STABLE text first (helps implicit prompt caching); the
# variable catalogue + citizen message are appended in the user turn.
# ---------------------------------------------------------------------------

_RESOLVER_SYSTEM = (
    "You are a JSON-only API that maps an Indonesian citizen's free-text "
    "request to the correct SIAP licence. SIAP is DPMPTSP Central Java's "
    "licensing system. You are given the COMPLETE licence catalogue as lines "
    "of `id | name | sektor`.\n"
    "\n"
    "Understand the INTENT, do not match strings literally:\n"
    "  - Ignore filler/politeness ('saya mau', 'ajukan', 'izin', 'tolong', "
    "'mohon', 'permohonan', 'untuk usaha saya', 'please').\n"
    "  - Expand acronyms to their licence name (e.g. PKPP = Persetujuan "
    "Pengadaan Kapal Perikanan).\n"
    "  - Tolerate typos, word-order differences, and synonyms.\n"
    "\n"
    "Pick the SINGLE best licence. If several catalogue entries are genuinely "
    "plausible for the request, list the others as alternatives (most likely "
    "first). If NOTHING in the catalogue fits, return null for the best id and "
    "an empty alternatives list. NEVER invent an id that is not in the "
    "catalogue.\n"
    "\n"
    "Output VALID JSON and absolutely nothing else — no Markdown, no code "
    "fences, no commentary. Exact schema:\n"
    '{"best_license_id": <int id from the catalogue, or null>, '
    '"alternatives": [<int ids from the catalogue>], '
    '"confidence": <number 0..1>}'
)


# ---------------------------------------------------------------------------
# Fuzzy-fallback tokenizer
# ---------------------------------------------------------------------------

# Indonesian filler that pollutes a licence query — drop before matching.
# Superset of the verbs/nouns the old regex stripped plus a few more.
_STOPWORDS = frozenset({
    "izin", "perizinan", "izinnya", "ajukan", "mengajukan", "ajuin",
    "pengajuan", "permohonan", "mohon", "daftar", "mendaftar", "daftarkan",
    "mendaftarkan", "saya", "aku", "mau", "ingin", "pengen", "hendak",
    "untuk", "usaha", "usahanya", "tolong", "bantu", "bantuin", "bikin",
    "buat", "buatkan", "urus", "uruskan", "proses", "proseskan", "sebuah",
    "please", "apply", "for", "the", "a", "an", "new", "submit", "start",
    "begin", "file", "permit", "license", "licence", "lisensi", "dong",
    "ya", "yg", "yang", "dan", "atau", "ke", "di", "nih", "kak", "pak",
    "bu", "min", "admin",
})

# Keep tokens this long or longer — drops "ke", "di", stray 1-2 char noise.
_MIN_TOKEN_LEN = 3


def _tokenize_for_fallback(message: str) -> list[str]:
    """Lowercase, split on non-word chars, drop stopwords + short tokens.

    Returns a de-duplicated, order-preserving list of content tokens to feed
    the fuzzy SQL. Empty list means "nothing to match on".
    """
    if not message:
        return []
    raw = re.split(r"[^0-9A-Za-zÀ-ÿ]+", message.lower())
    seen: set[str] = set()
    tokens: list[str] = []
    for t in raw:
        t = t.strip()
        if len(t) < _MIN_TOKEN_LEN:
            continue
        if t in _STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        tokens.append(t)
    return tokens


def _mask_msg(message: str) -> str:
    """A length-only, content-free marker for logs.

    The citizen message can carry a business name (PII), so we never log it at
    INFO. We log only its character length, which is enough to debug the
    resolver path without leaking anything.
    """
    return f"<len={len(message or '')}>"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# The "couldn't resolve" note _start_session shows the citizen.
_NO_MATCH_NOTE = (
    "Maaf, saya belum menemukan izin yang cocok. "
    "Coba sebutkan nama izinnya lebih spesifik."
)


async def resolve_license_intent(message: str) -> dict:
    """
    Resolve a citizen's free-text request to SIAP licence match(es).

    Returns the SAME shape `siap_tools.siap_lookup_license` returns so
    `guided_submission._start_session` can consume it unchanged:

      {
        "found": bool,
        "matches": [{"license_id": int, "name": str, "sektor": str|None}, ...],
        "note": str | None,
      }

    Strategy:
      1. Load the cached licence catalogue (TTL cache in siap_tools).
      2. If empty (SIAP DB down) → fuzzy SQL fallback over the catalogue is
         pointless (no catalogue), so we go to the DB-backed fuzzy fallback
         which itself returns [] when SIAP is down → found=False.
      3. Otherwise ask Gemini to map the message to the best licence id(s),
         grounded in the catalogue. Validate ids against the catalogue, enrich,
         and return.
      4. On any Gemini error / timeout / unparseable output → fuzzy fallback.

    NEVER raises.
    """
    from services.siap_tools import get_license_catalogue

    msg = (message or "").strip()
    if not msg:
        return {"found": False, "matches": [], "note": _NO_MATCH_NOTE}

    catalogue = await get_license_catalogue()
    if not catalogue:
        # No catalogue to ground the LLM on (SIAP DB down/unconfigured). The
        # fuzzy fallback reads SIAP directly, so it will also return nothing —
        # but routing through it keeps a single, consistent failure path.
        logger.info(
            "resolve_license_intent: empty catalogue — fuzzy fallback | msg=%s",
            _mask_msg(msg),
        )
        return await _fuzzy_fallback(msg, reason="empty-catalogue")

    # Index the catalogue by id for O(1) validation + enrichment.
    by_id: dict[int, dict] = {c["license_id"]: c for c in catalogue}

    try:
        llm_out = await _call_llm_resolver(msg, catalogue)
    except Exception:
        logger.warning(
            "resolve_license_intent: LLM call failed — fuzzy fallback | msg=%s",
            _mask_msg(msg),
        )
        return await _fuzzy_fallback(msg, reason="llm-error")

    if llm_out is None:
        # Unparseable / malformed JSON from the model.
        logger.warning(
            "resolve_license_intent: LLM output unusable — fuzzy fallback | msg=%s",
            _mask_msg(msg),
        )
        return await _fuzzy_fallback(msg, reason="llm-unparseable")

    best = llm_out.get("best_license_id")
    alternatives = llm_out.get("alternatives") or []

    # Build ordered candidate id list = [best] + alternatives, validating each
    # against the catalogue (drops hallucinated ids) and de-duping.
    ordered_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in [best, *alternatives]:
        vid = _coerce_id(raw_id)
        if vid is None or vid not in by_id or vid in seen:
            continue
        seen.add(vid)
        ordered_ids.append(vid)
        if len(ordered_ids) >= 1 + _MAX_ALTERNATIVES:
            break

    if not ordered_ids:
        # Model said "nothing fits" (or every id it returned was a
        # hallucination). Honour that as a clean miss — do NOT fall back to the
        # fuzzy matcher, which would only re-introduce the brittle substring
        # behaviour this resolver exists to replace.
        logger.info(
            "resolve_license_intent: LLM found no valid match | msg=%s conf=%s",
            _mask_msg(msg), llm_out.get("confidence"),
        )
        return {"found": False, "matches": [], "note": _NO_MATCH_NOTE}

    matches = [
        {
            "license_id": by_id[i]["license_id"],
            "name": by_id[i]["name"],
            "sektor": by_id[i].get("sektor"),
        }
        for i in ordered_ids
    ]
    logger.info(
        "resolve_license_intent: LLM path | msg=%s matches=%d conf=%s",
        _mask_msg(msg), len(matches), llm_out.get("confidence"),
    )
    return {"found": True, "matches": matches, "note": None}


# ---------------------------------------------------------------------------
# LLM call — reuses ai_handler's Gemini client + JSON handling.
# ---------------------------------------------------------------------------

def _format_catalogue_block(catalogue: list[dict]) -> str:
    """Render the catalogue as `id | name | sektor` lines for the prompt."""
    lines = []
    for c in catalogue:
        sektor = c.get("sektor") or "-"
        lines.append(f"{c['license_id']} | {c['name']} | {sektor}")
    return "\n".join(lines)


async def _call_llm_resolver(message: str, catalogue: list[dict]) -> Optional[dict]:
    """
    Call Gemini to map `message` → licence id(s), grounded in `catalogue`.

    Reuses `ai_handler._call_gemma` (same REST client/model/config as
    `analyze_user_intent`) and `ai_handler._strip_json_fences`. Returns the
    parsed dict {"best_license_id", "alternatives", "confidence"} or None if
    the response can't be parsed as the expected JSON object. Re-raises the
    underlying call's exceptions (network/HTTP) so the caller routes to the
    fuzzy fallback.
    """
    from services.ai_handler import _call_gemma, _strip_json_fences

    catalogue_block = _format_catalogue_block(catalogue)
    # STABLE grounding first (the catalogue is the long, cache-friendly block),
    # the VARIABLE citizen request last.
    user_turn = (
        "LICENCE CATALOGUE (id | name | sektor):\n"
        f"{catalogue_block}\n"
        "\n"
        "CITIZEN REQUEST:\n"
        f"{message}\n"
        "\n"
        "Return only the JSON object."
    )

    raw = await _call_gemma(
        _RESOLVER_SYSTEM,
        user_turn,
        max_tokens=_RESOLVER_MAX_TOKENS,
        temperature=0.0,
        model_override=_RESOLVER_MODEL,
        disable_thinking=True,  # Gemini 2.5 thinking otherwise eats the budget → truncated JSON
    )
    cleaned = _strip_json_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    # Normalise the two list/scalar fields defensively.
    if "alternatives" in parsed and not isinstance(parsed["alternatives"], list):
        parsed["alternatives"] = []
    return parsed


def _coerce_id(value: Any) -> Optional[int]:
    """Best-effort coerce a model-returned id to int. None on anything odd."""
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"\d+", s):
            return int(s)
    return None


# ---------------------------------------------------------------------------
# Fuzzy SQL fallback — parameterised, read-only, over the LICENSE catalogue.
# ---------------------------------------------------------------------------

_FUZZY_LIMIT = 8


async def _fuzzy_fallback(message: str, *, reason: str) -> dict:
    """
    Resolve by fuzzy token match when the LLM path is unavailable.

    Tokenize the message, drop stopwords, keep tokens len>=3, then:
      1. AND-match: every token must appear (ILIKE substring) in the name.
      2. If that yields nothing, OR-match: any token appears.
    Every token is a bound parameter ($1, $2, …) — no string interpolation
    into SQL. Returns the same shape as the LLM path. Returns found=False on
    no tokens / SIAP down / no match.
    """
    from services.siap_db import get_siap_pool, is_siap_db_configured

    tokens = _tokenize_for_fallback(message)
    if not tokens:
        logger.info(
            "resolve_license_intent: fuzzy fallback no tokens | reason=%s msg=%s",
            reason, _mask_msg(message),
        )
        return {"found": False, "matches": [], "note": _NO_MATCH_NOTE}

    if not is_siap_db_configured():
        logger.info(
            "resolve_license_intent: fuzzy fallback — SIAP DB not configured | "
            "reason=%s msg=%s",
            reason, _mask_msg(message),
        )
        return {"found": False, "matches": [], "note": _NO_MATCH_NOTE}

    # Cap token count so a pathological message can't build a huge predicate.
    tokens = tokens[:6]

    base = (
        "SELECT c.license_id, c.name, p.name AS sektor "
        "FROM ptsp.license c "
        "LEFT JOIN ptsp.license p ON p.license_id = c.parent_id "
        "WHERE c.stereotype = 'LICENSE' AND ({clause}) "
        "ORDER BY length(c.name) ASC, c.name ASC "
        f"LIMIT {_FUZZY_LIMIT}"
    )
    # Each token → "c.name ILIKE '%' || $N || '%'". Parameters are the tokens.
    conds = [f"c.name ILIKE '%' || ${i + 1} || '%'" for i in range(len(tokens))]
    sql_and = base.format(clause=" AND ".join(conds))
    sql_or = base.format(clause=" OR ".join(conds))

    try:
        pool = await get_siap_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql_and, *tokens)
            path = "and"
            if not rows:
                rows = await conn.fetch(sql_or, *tokens)
                path = "or"
        matches = [
            {
                "license_id": r["license_id"],
                "name": r["name"],
                "sektor": r["sektor"],
            }
            for r in rows
        ]
        logger.info(
            "resolve_license_intent: fuzzy fallback | reason=%s match=%s "
            "tokens=%d matches=%d msg=%s",
            reason, path, len(tokens), len(matches), _mask_msg(message),
        )
        if not matches:
            return {"found": False, "matches": [], "note": _NO_MATCH_NOTE}
        return {"found": True, "matches": matches, "note": None}
    except Exception:  # pragma: no cover — defensive; never break the caller
        logger.exception(
            "resolve_license_intent: fuzzy fallback query failed | reason=%s msg=%s",
            reason, _mask_msg(message),
        )
        return {"found": False, "matches": [], "note": _NO_MATCH_NOTE}
