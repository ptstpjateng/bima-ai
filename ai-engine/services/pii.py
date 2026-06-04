"""Free-text PII masking for citizen- and officer-facing rendered output.

WHY this exists (and why it's separate from `logging_setup.mask_pii`):
  `logging_setup.mask_pii` scrubs *structured log fields* — it only fires on
  KEY=VALUE shapes (`nik=…`, `msisdn=…`, `user_id=wa-…`). That's exactly right
  for our own log lines, but useless for the thing this module guards: the
  free-text *evidence quotes* Gemini Vision returns when it reads a document.

  A SuitabilityFinding's `evidence` is a verbatim-ish quote of the document,
  e.g. "…PROVINSI JAWA TENGAH KABUPATEN PEMALANG NIK : 3327080511740081".
  That string is rendered into BOTH the citizen score message
  (`citizen_scorer.render_score_message`) and the officer brief / copilot
  validation context (`officer_bridge`). The masked review block already shows
  NIK as "33******81"; the evidence quote did not, so the full 16-digit NIK
  (and phone numbers) leaked into both surfaces.

  This module redacts NIK runs and Indonesian phone-shaped runs *anywhere in
  free text*, in the same first-2 / last-2 visible style as the existing
  review block, so the two surfaces match.

DESIGN:
  * Zero imports from `services.*` — both `citizen_scorer` and `officer_bridge`
    import it with no circular-dependency risk (it's a leaf).
  * Conservative by construction: only 16-digit NIK runs and phone-shaped runs
    are touched. Dates, short IDs, licence/ticket numbers (≤ ~9 digits),
    confidence percentages, etc. are left untouched.
  * Idempotent: a value already masked as "33******81" or "08****12" has no
    16-digit / phone-shaped run left to match, so re-running is a no-op (no
    awkward double-masking).
"""

from __future__ import annotations

import re

__all__ = ["mask_pii", "mask_nik", "mask_phone"]


# ---------------------------------------------------------------------------
# NIK — Indonesian National ID: EXACTLY 16 digits, often printed with spaces
# or dots between groups ("3327 0805 1174 0081", "3327.0805.1174.0081") and
# frequently embedded after a label ("NIK : 3327080511740081").
#
# We match a run of digits-with-optional-single-separators that contains
# EXACTLY 16 digits. The boundaries reject a 15- or 17-digit neighbour so a
# longer number can't be partially eaten:
#   * left:  not preceded by a digit or a digit+separator
#   * right: not followed by a digit or a separator+digit
# Separators allowed *inside* the run: a single space or dot between groups.
# ---------------------------------------------------------------------------

# A 16-digit run, optionally grouped by single space/dot separators.
# (?<![\d.\s\d]) is not expressible directly; we use explicit lookarounds.
_NIK_RE = re.compile(
    r"(?<!\d)"                     # not mid-number on the left
    r"(?<![\d][ .])"              # not "<digit><sep>" immediately before (would extend a longer run)
    r"(\d[\d .]{14,30}\d)"        # candidate run: starts+ends with a digit, separators allowed inside
    r"(?![ .]?\d)"                # not followed by an (optional sep +) digit on the right
)


def _mask_middle(digits: str, *, keep_head: int, keep_tail: int, fill: str = "*") -> str:
    """Keep the first `keep_head` and last `keep_tail` digits; mask the middle.

    The mask width equals the number of hidden digits, matching the existing
    "33******81" (NIK: 2 + 12 hidden + 2) / "08****12" styles.
    """
    n = len(digits)
    if n <= keep_head + keep_tail:
        # Too short to mask meaningfully — leave as-is (shouldn't happen for
        # the callers below, which gate on length first).
        return digits
    hidden = n - keep_head - keep_tail
    return digits[:keep_head] + (fill * hidden) + digits[-keep_tail:]


def _nik_sub(m: re.Match[str]) -> str:
    """Mask a matched NIK run IFF it contains exactly 16 digits.

    The regex over-matches slightly (any 14–32 char digit/sep run); we count
    the actual digits here and only mask when it's exactly 16 — so a grouped
    phone, a long account number, etc. that happens to fall in the same shape
    is not mistaken for a NIK. Separators in the original run are dropped from
    the masked output (the masked token is digits + stars only, like the
    review block's "33******81").
    """
    run = m.group(1)
    digits = re.sub(r"\D", "", run)
    if len(digits) != 16:
        return run  # not a NIK — leave the original text untouched
    return _mask_middle(digits, keep_head=2, keep_tail=2)


def mask_nik(text: str) -> str:
    """Redact 16-digit NIK runs (with optional space/dot grouping) in free text.

    "NIK : 3327080511740081"      -> "NIK : 33************81"
    "3327 0805 1174 0081"         -> "33************81"
    Leaves 15-/17-digit and shorter runs alone.
    """
    if not text:
        return text
    return _NIK_RE.sub(_nik_sub, text)


# ---------------------------------------------------------------------------
# Indonesian phone numbers.
#
# Two common shapes, embedded anywhere in free text:
#   * local mobile:  08\d{7,12}            (08123456789)
#   * E.164(-ish):   +?62\d{7,12}          (+6285117557091 / 6285117557091)
# Bounded by non-digits so we don't slice a longer number, and ANCHORED so a
# 16-digit NIK starting with neither 08 nor 62 is handled by mask_nik instead.
#
# Masking keeps a short head (2–4 chars, incl. the +) and the last 2, e.g.
# "+62**********91", "08*******89" — same visible-ends style as the NIK mask.
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(\+?62\d{7,12}|08\d{7,12})"
    r"(?!\d)"
)


def _phone_sub(m: re.Match[str]) -> str:
    raw = m.group(1)
    has_plus = raw.startswith("+")
    digits = raw[1:] if has_plus else raw
    # Head: keep the country/operator prefix so an operator can still tell a
    # +62 from an 08 number during an incident. 62-numbers keep 2 leading
    # digits, 08-numbers keep 2 leading digits; the leading "+" is preserved
    # on top of that. Tail: last 2 digits.
    keep_head = 2
    keep_tail = 2
    if len(digits) <= keep_head + keep_tail:
        return raw
    masked = _mask_middle(digits, keep_head=keep_head, keep_tail=keep_tail)
    return ("+" if has_plus else "") + masked


def mask_phone(text: str) -> str:
    """Redact Indonesian phone-shaped runs (08… / +62… / 62…) in free text.

    "hubungi 085117557091"   -> "hubungi 08*******91"
    "+6285117557091"         -> "+62**********91"
    """
    if not text:
        return text
    return _PHONE_RE.sub(_phone_sub, text)


# ---------------------------------------------------------------------------
# Public combined entrypoint.
# ---------------------------------------------------------------------------


def mask_pii(text: str) -> str:
    """Redact NIK and phone-shaped runs in arbitrary free text.

    Order: NIK first (it's the strictly-16-digit, non-08/62-prefixed case in
    practice), then phones. Both passes are conservative and idempotent, so
    running this over already-masked text (e.g. a review block that already
    shows "33******81") is a no-op.

    Safe on `None`/empty (returns the input unchanged) so callers can pass a
    possibly-missing field without a guard.
    """
    if not text:
        return text
    return mask_phone(mask_nik(text))
