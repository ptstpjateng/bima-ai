"""Heuristic prompt-injection detector.

Why this exists
---------------
This module does NOT block messages. It is an *audit* layer: cheap regex
heuristics flag suspicious phrases (the kind that show up in jailbreak
attempts — "ignore previous instructions", "you are now …", "system
prompt", "<|im_start|>") so we can:

  (a) log a WARNING with the masked user id + which pattern matched,
  (b) count per-user hits to escalate to ERROR when a single user trips
      the heuristics repeatedly in a short window (>3 in an hour ⇒ likely
      a deliberate probe, not a typo).

The reason we *don't* refuse the message is that Indonesian citizens may
legitimately type things like "tolong abaikan pertanyaan saya tadi" or
"anda adalah BIMA, kan?" — strict blocking has unacceptable false-positive
rate for a government service. Defence-in-depth is handled by the LLM-side
hardening in `ai_handler._SYSTEM_PROMPT` (the "Aturan keamanan WAJIB" block)
and the citizen-vs-officer tool-registry isolation. This module exists to
give the security team *visibility* into attack volume.

Patterns are matched on the *post-sanitization* string (after
`input_sanitizer.sanitize_user_input`) so confusables and bidi tricks have
already been collapsed. Matches are case-insensitive.

Public API
----------
* `detect_injection_patterns(text) -> list[str]`
    Pure / sync. Returns the list of matched pattern names (empty if clean).
    No logging, no state. Useful for unit tests and the audit caller below.

* `audit_user_input(user_id, text) -> None`
    Side-effecting wrapper. Calls the detector; if any patterns matched,
    logs at WARNING with masked user id + the pattern names, increments the
    per-user counter, and escalates to ERROR if the rolling-hour count
    crosses the threshold.

The per-user counter is in-memory (deque per user, hour window). This is
intentionally not persisted: false-positives are common, and a process
restart resetting the count is acceptable for an audit signal. The
dispatcher process is long-lived in practice (Docker compose unit), so a
deliberate attacker still trips the threshold within a single restart
window.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from threading import Lock

logger = logging.getLogger("bima_ai.prompt_injection")

# Window + threshold for escalation. >3 matches in a rolling hour means
# WARNING upgrades to ERROR for that match. Tunable via constants — not env
# vars, because the threshold should not change between environments
# (debugging an audit pattern is much easier with consistent behaviour).
_WINDOW_SECONDS = 3600.0
_ESCALATION_THRESHOLD = 3

# Patterns. Each entry is (name, compiled_pattern). Names are stable strings
# that appear in logs — chose them so a grep on logs surfaces every variant of
# a given attack family (e.g. all `role_hijack_*` rows).
#
# Sources for the catalogue:
#   - PromptArmor / LangChain / Lakera public injection corpora (2024–25)
#   - HuggingFace `prompt-injection` datasets
#   - The actual jailbreaks we saw during BIMA preview testing (April 2026)
#
# Each pattern is intentionally broad — false positives are acceptable here
# because we log + count, we do not block. Add to this list; do not narrow it
# without a documented reason.
_RAW_PATTERNS: list[tuple[str, str]] = [
    # --- Direct override attempts ---------------------------------------
    ("override_ignore_previous",
     r"\b(ignore|abaikan|lupakan|forget|disregard)\b[^.\n]{0,40}\b"
     r"(previous|sebelumnya|all|semua|above|instructions?|instruksi|"
     r"prompts?|petunjuk|aturan|rules?|system)\b"),

    ("override_new_instructions",
     r"\b(new|baru|berikut)\s+(instructions?|instruksi|prompt|aturan|rules?)\b"),

    ("override_developer_mode",
     r"\b(developer\s*mode|dev\s*mode|debug\s*mode|admin\s*mode|"
     r"mode\s+pengembang|mode\s+admin)\b"),

    # --- System prompt leak attempts ------------------------------------
    ("leak_system_prompt",
     r"\b(system\s*prompt|prompt\s+sistem|initial\s+prompt|"
     r"instruksi\s+(awal|sistem)|hidden\s+(prompt|instructions?))\b"),

    ("leak_reveal_show",
     r"\b(reveal|show|print|tampilkan|tunjukkan|cetak|berikan|tulis(kan)?)\b"
     r"[^.\n]{0,40}\b(prompt|instructions?|instruksi|system|rules?|aturan|"
     r"persona|persona\s*mu|kepribadian)\b"),

    ("leak_repeat_above",
     r"\b(repeat|ulangi|copy|salin)\b[^.\n]{0,30}\b(above|atas|"
     r"everything|semua\s+yang)\b"),

    # --- Role hijack / persona swap -------------------------------------
    ("role_hijack_you_are_now",
     r"\b(you\s+are\s+now|kamu\s+sekarang|kau\s+sekarang|anda\s+sekarang|"
     r"sekarang\s+kamu|act\s+as|berperan\s+sebagai|pura[- ]pura|berpura[- ]pura|"
     r"pretend\s+(to\s+be|you\s+are))\b"),

    ("role_hijack_dan",
     r"\b(DAN|do\s+anything\s+now|jailbreak|jailbroken|"
     r"unrestricted|tanpa\s+batasan|tanpa\s+filter)\b"),

    ("role_hijack_ai_swap",
     r"\b(you\s+are\s+(chatgpt|gpt|claude|gemini|bard|llama|copilot)|"
     r"kamu\s+adalah\s+(chatgpt|gpt|claude|gemini|bard|llama)|"
     r"behave\s+like\s+(chatgpt|gpt|claude))\b"),

    # --- Chat-template / control-token smuggling ------------------------
    # These are the special tokens used by various model families to
    # delimit roles. If a citizen message contains one, it's either a
    # paste from a debugging session or an injection attempt — either way
    # worth flagging.
    ("control_token_im_start",
     r"<\|im_(start|end)\|>"),

    ("control_token_endoftext",
     r"<\|(endoftext|end_of_text|eot_id|start_header_id|end_header_id)\|>"),

    ("control_token_role_marker",
     r"<\|(system|user|assistant|tool)\|>"),

    ("control_token_chatml",
     r"\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>"),

    # --- Role markers in plaintext --------------------------------------
    # An attacker often pastes "SYSTEM: do X" hoping the model treats it
    # as a real role line. Distinct from `control_token_*` which catch
    # the tokenizer-special variants.
    ("plaintext_role_marker",
     r"(?im)^(system|user|assistant|tool)\s*:\s*\S"),

    # --- Tool / function-call abuse -------------------------------------
    ("tool_misuse_function",
     r"\b(call|invoke|execute|jalankan|panggil)\s+(function|tool|fungsi)\b"),

    ("tool_misuse_admin",
     r"\b(admin|root|sudo|superuser|administrator)\b[^.\n]{0,40}\b"
     r"(access|akses|privilege|hak|password|kunci)\b"),
]


_COMPILED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in _RAW_PATTERNS
]


def detect_injection_patterns(text: str) -> list[str]:
    """Return the names of every pattern that matches `text`.

    Empty list ⇒ message looked clean to our heuristics. Pure / sync / no I/O.
    Intended for use both inside `audit_user_input` and in unit tests, where
    a stable return value is much nicer than parsing log lines.
    """
    if not text:
        return []
    return [name for name, regex in _COMPILED_PATTERNS if regex.search(text)]


# Per-user rolling-hour counters. Keyed by the channel-prefixed user_id
# (e.g. "wa-628…", "tg-1234…", "web-uuid…"). Deque of float timestamps;
# expiry happens lazily at read time.
_user_hits: dict[str, deque[float]] = defaultdict(deque)
_user_hits_lock = Lock()


def _mask_user_id(user_id: str) -> str:
    """Mask the identifier part of a channel-prefixed user_id for safe logging.

    Mirrors the masking patterns used elsewhere (`whatsapp_sender._mask`,
    `aptana._mask`) but defensive about unknown shapes: any string shorter
    than 8 chars after the prefix is rendered as "<short>" rather than
    risking PII leak.

    Examples:
      "wa-6281234567890"  → "wa-6281…7890"
      "tg-12345"          → "tg-<short>"
      "anonymous"         → "anonymous" (no separator ⇒ assume non-PII)
    """
    if not user_id or "-" not in user_id:
        return user_id or "<empty>"
    prefix, _, ident = user_id.partition("-")
    if len(ident) < 8:
        return f"{prefix}-<short>"
    return f"{prefix}-{ident[:4]}…{ident[-4:]}"


def _record_hit_and_get_count(user_id: str, now: float | None = None) -> int:
    """Append a hit timestamp, drop expired ones, return current count."""
    now = now if now is not None else time.time()
    cutoff = now - _WINDOW_SECONDS
    with _user_hits_lock:
        bucket = _user_hits[user_id]
        # Drop expired entries from the left (oldest first).
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)
        return len(bucket)


def audit_user_input(user_id: str, text: str) -> list[str]:
    """Run the detector; log + count if anything matched.

    Returns the list of matched pattern names (same as
    `detect_injection_patterns`) so callers that need to react (e.g. add
    metadata to a trace) can. The default citizen-channel callers just
    invoke this for the side effect and ignore the return value.

    Logging policy:
      * 0 matches  → nothing logged (we don't want a per-message hot loop
        of "clean" lines).
      * ≥1 match   → WARNING with user id (masked) + matched patterns +
        rolling-hour count for this user.
      * count > _ESCALATION_THRESHOLD ⇒ a second log line at ERROR level
        flagging the user as a likely deliberate probe. We do not block —
        downstream rate-limiter already throttles them.
    """
    matches = detect_injection_patterns(text)
    if not matches:
        return matches

    count = _record_hit_and_get_count(user_id)
    safe_id = _mask_user_id(user_id)
    logger.warning(
        "prompt_injection_audit | user=%s | patterns=%s | hourly_count=%d",
        safe_id, ",".join(matches), count,
    )
    if count > _ESCALATION_THRESHOLD:
        logger.error(
            "prompt_injection_escalation | user=%s | patterns=%s | "
            "hourly_count=%d | threshold=%d — likely deliberate probe; "
            "review rate limiter / channel block list.",
            safe_id, ",".join(matches), count, _ESCALATION_THRESHOLD,
        )
    return matches


def _reset_state_for_tests() -> None:
    """Drop all in-memory counters. Tests only — never called in prod."""
    with _user_hits_lock:
        _user_hits.clear()
