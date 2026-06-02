"""Input sanitizer for untrusted text reaching the LLM prompt boundary.

Why this layer exists
---------------------
BIMA receives free-form text from citizens via Telegram, WhatsApp (APTANA + future
direct Meta), and the public portal. Every byte of that text eventually becomes
part of a Gemma/Gemini prompt. The model itself is the LAST line of defence
against prompt injection — this module is the FIRST: cheap, deterministic, and
applied at the router boundary before the message is even queued.

What it removes / normalises
----------------------------
1. ASCII control characters (U+0000–U+001F, U+007F) — except the three we
   legitimately allow inside a chat bubble: TAB (U+0009), LF (U+000A), CR
   (U+000D). Anything else is a "smuggled" control byte (NUL, BEL, ESC) and
   has no place in a citizen WhatsApp message.

2. Bidirectional override / format marks — U+202A..U+202E and U+2066..U+2069.
   These are the classic Trojan Source vectors: they can visually re-order text
   so the human-readable message disagrees with the bytes the LLM sees. We
   strip them outright.

3. BOM (U+FEFF) and other zero-width formatting characters that serve no
   purpose in chat (ZWSP / ZWNJ / ZWJ — U+200B..U+200D). Same rationale:
   invisible bytes that can encode hidden instructions.

4. Unicode NFKC normalisation — collapses confusable forms (e.g. fullwidth
   "ＩＧＮＯＲＥ" → "IGNORE") so the downstream detector sees the same string a
   human would read aloud.

5. Length cap of 4096 chars. WhatsApp text messages cap at 4096 bytes;
   Telegram at 4096 chars; both BSPs reject anything longer. A single message
   longer than that is either an attack payload or a paste accident — truncate
   with a marker so the LLM knows the input was clipped.

6. Long contiguous base64-like blobs (>200 chars of [A-Za-z0-9+/=]). Real
   citizen messages don't contain 200-char unbroken alphanumeric runs. A long
   blob almost always means encoded jailbreak text or smuggled tool output;
   we replace it with a [REDACTED-LONG-ENCODED-BLOB] marker so the LLM still
   sees the surrounding sentence but can't be steered by the payload.

We DO NOT
---------
* Filter Indonesian or English words on a blocklist. False positives are a
  worse UX than the rare jailbreak, and the LLM-side defences in ai_handler
  handle adversarial *intent* — not our job here.
* Strip emojis, accents, or non-Latin scripts. Indonesian users write in
  campuran (mixed) script and routinely use emojis; we MUST leave them
  intact.
* Trim trailing whitespace beyond what `.strip()` removes. Internal
  whitespace patterns can be meaningful (numbered lists, code blocks).

The function is pure / sync / O(n). Safe to call on every inbound message.
"""

from __future__ import annotations

import re
import unicodedata

# Max chars after normalisation. Mirrors WhatsApp (4096 bytes ≈ chars for
# Latin script; for emoji-heavy text WhatsApp itself enforces the byte cap
# before we ever see the message) and Telegram (4096 chars).
MAX_LENGTH = 4096

# Truncation marker. Visible enough that the LLM understands the input was
# clipped, but doesn't smell like a real instruction.
_TRUNCATE_MARKER = "… [pesan dipotong karena terlalu panjang]"

# Bidirectional override / isolate format characters. These are the Trojan
# Source vectors.  See https://trojansource.codes/ — same idea applies in any
# Unicode-aware system, not just compilers.
_BIDI_CHARS = {
    "‪",  # LRE Left-to-Right Embedding
    "‫",  # RLE Right-to-Left Embedding
    "‬",  # PDF Pop Directional Formatting
    "‭",  # LRO Left-to-Right Override
    "‮",  # RLO Right-to-Left Override
    "⁦",  # LRI Left-to-Right Isolate
    "⁧",  # RLI Right-to-Left Isolate
    "⁨",  # FSI First Strong Isolate
    "⁩",  # PDI Pop Directional Isolate
}

# Zero-width / BOM characters that are invisible to humans but visible to the
# tokenizer. Splitting "ig​nore" into "ig" + ZWSP + "nore" is a known
# trick to defeat naive blocklists; NFKC doesn't merge them, so we drop them.
_ZERO_WIDTH_CHARS = {
    "​",  # ZWSP
    "‌",  # ZWNJ
    "‍",  # ZWJ — note: this is also a legitimate emoji ZWJ-sequence joiner
    "﻿",  # BOM
}

# Build a single translation table for the strippable codepoints. dict.fromkeys
# with None deletes them. Whitelisted control chars (TAB/LF/CR) are NOT in
# this table — they survive.
_STRIP_TRANSLATION: dict[int, None] = {}
for _c in _BIDI_CHARS:
    _STRIP_TRANSLATION[ord(_c)] = None
# NOTE on ZWJ (U+200D): stripping it breaks compound emojis like
# "👨‍👩‍👧" (which is "man + ZWJ + woman + ZWJ + girl"). We accept this
# trade-off — the attack value of ZWJ outweighs emoji families in a perizinan
# chatbot. If complaints come in, switch to a "keep ZWJ between emoji
# codepoints, drop elsewhere" rule.
for _c in _ZERO_WIDTH_CHARS:
    _STRIP_TRANSLATION[ord(_c)] = None

# Control chars to drop: everything in U+0000–U+001F + U+007F EXCEPT TAB/LF/CR.
# Build the list explicitly so the intent is auditable.
_ALLOWED_CONTROL = {0x09, 0x0a, 0x0d}  # TAB, LF, CR
for _i in range(0x00, 0x20):
    if _i not in _ALLOWED_CONTROL:
        _STRIP_TRANSLATION[_i] = None
_STRIP_TRANSLATION[0x7f] = None  # DEL

# Detect contiguous base64-like runs (>200 chars). Real citizen messages
# don't have unbroken 200-char alphanumeric strings; encoded payloads do.
# Pattern is intentionally permissive on padding ('=') and URL-safe variants
# ('-', '_') because attackers freely mix them.
_LONG_BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/=_-]{200,}")
_REDACTED_BLOB_MARKER = "[REDACTED-LONG-ENCODED-BLOB]"


def sanitize_user_input(text: str) -> str:
    """Sanitize a raw user message before it is shown to the LLM.

    Order matters:
      1. NFKC normalise — collapse confusable / compatibility forms early so
         every later step sees a canonical string.
      2. Strip control + bidi + zero-width via single translation table.
      3. Replace long base64-like blobs with a redaction marker.
      4. Length cap (after stripping — otherwise stripped chars eat budget).
      5. Final `.strip()` to drop leading/trailing whitespace.

    Returns an empty string if the input collapses to nothing (e.g. message
    was 100 % control bytes). Callers should treat empty output the same as
    a missing message — drop it, don't ship to the LLM.

    Idempotent: sanitize(sanitize(x)) == sanitize(x).
    Pure: no I/O, no logging — the caller decides whether to log a delta.
    """
    if not isinstance(text, str):
        # Defensive: routers should pass str, but some webhook bodies arrive
        # as bytes/None. Return empty so the caller drops the message.
        return ""

    # Step 1 — Unicode normalisation. NFKC, not NFC: we want "ＩＧＮＯＲＥ" to
    # become "IGNORE" so the detector heuristics can fire on the canonical
    # form. NFKC collapses fullwidth, ligatures, and other compatibility
    # variants.
    normalised = unicodedata.normalize("NFKC", text)

    # Step 2 — drop banned control / bidi / zero-width codepoints.
    cleaned = normalised.translate(_STRIP_TRANSLATION)

    # Step 3 — redact long encoded blobs. Done AFTER zero-width stripping
    # so an attacker can't break the pattern with hidden separators.
    cleaned = _LONG_BASE64_PATTERN.sub(_REDACTED_BLOB_MARKER, cleaned)

    # Step 4 — length cap. Truncate with marker so the LLM sees the message
    # was clipped (and won't try to "continue" half a sentence).
    if len(cleaned) > MAX_LENGTH:
        # Reserve room for the marker so the final string is <= MAX_LENGTH.
        keep = MAX_LENGTH - len(_TRUNCATE_MARKER)
        if keep < 0:
            # Marker is longer than the cap (shouldn't happen). Hard-truncate.
            cleaned = cleaned[:MAX_LENGTH]
        else:
            cleaned = cleaned[:keep] + _TRUNCATE_MARKER

    # Step 5 — trim outer whitespace only. Internal whitespace is meaningful.
    return cleaned.strip()
