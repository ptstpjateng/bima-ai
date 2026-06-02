# Decision note — Native WhatsApp typing + read receipts (Decisions §9 addendum)

> **Where this belongs:** the canonical Decisions log lives in the external
> private vault at `../BIMA-Vault/Decisions.md` (§9 — "WhatsApp typing
> acknowledgment"). That vault is NOT checked into this repo, so this in-repo
> note ships with the PR as the engineering record. The text below should ALSO
> be appended as a sub-section under §9 in the vault. It does **not** rewrite or
> replace the locked text-bubble decision — it records the new, feature-flagged
> native path layered on top of it.

## §9 addendum — native typing+read implemented behind a flag (2026-06-02)

**Status:** code-complete, shipped OFF by default. Flag: `BIMA_NATIVE_TYPING_ENABLED`.

**What changed.** The "💭 Sebentar ya, BIMA sedang mencarikan info untukmu…"
text bubble remains the locked default acknowledgment. We added an *optional*
native path that, when enabled and configured, calls Meta's Graph API directly
to mark the citizen's inbound message **read** (blue double-ticks) **and** show
the native **"…" typing indicator** — both in a single call:

```
POST https://graph.facebook.com/<META_GRAPH_VERSION>/<META_WA_PHONE_NUMBER_ID>/messages
{ "messaging_product": "whatsapp", "status": "read",
  "message_id": "<wamid>", "typing_indicator": {"type": "text"} }
```

New module `ai-engine/services/meta_whatsapp_sender.py` exposes only
`mark_read_and_typing(message_id, *, show_typing=True)`. It is **status-only** —
it never sends content, is **not** registered as a router in `main.py`, and
APTANA stays the sole outbound sender. `acknowledge_received()` keeps its exact
signature and `routers/aptana.py:188` call site; it is still fire-and-forget and
never raises.

**Why this can't go through APTANA.** APTANA's BSP wrapper 422-rejected Meta's
`status` / `message_id` / `typing_indicator` fields (May 2026 probe) — it
enforces a message-TYPE whitelist with no status channel. Meta-direct is the
only path that yields the native UX. (Locked text-bubble decision unchanged.)

**The unresolved blocker (why the flag stays OFF for June 4).** The read+typing
endpoint is the *same* `/{phone_number_id}/messages` endpoint as sending, and it
only accepts a token issued by the ONE Meta app the number is **registered** to.
BIMA's number `6285117557091` is currently registered to **APTANA's** app
(APTANA is the BSP). DPMPTSP having Business-Manager *admin* on the WABA grants
management rights (templates, analytics, sharing) but **not** per-number
messaging rights while APTANA owns the registration. So a DPMPTSP System User
token will likely fail this call with error `190` / "registered to another app".
We cannot confirm this with a live call (hard constraint), so we do **not**
claim native typing will work on June 4.

**Behavior matrix (no regression, ever):**

| Condition | Result |
|---|---|
| Flag unset / `false` | Text bubble (exactly as today) |
| Flag `true`, `META_WA_*` unset | Text bubble + a single INFO hint logged once |
| Flag `true`, configured, `message_id` missing | Text bubble |
| Flag `true`, configured, Meta returns 200 | Native read+typing; text bubble skipped (clean chat) unless `BIMA_NATIVE_TYPING_KEEP_TEXT_BUBBLE=true` |
| Flag `true`, configured, Meta non-200 / error 190 / timeout / exception | Logged masked at WARNING, falls back to text bubble |

**Decision gate for June 4.** Run ONE controlled live test to the engineer's
*own* WhatsApp number (never a citizen). If native ticks + typing appear →
leave `BIMA_NATIVE_TYPING_ENABLED=true`. If the log shows the
registered-to-another-app error → set it back to `false` and demo on the proven
text bubble. Either way the code is shipped and self-disables safely. The
notification engagement gate, 2/day cap, and STOP/opt-out safeguards are
untouched.

**Env vars (set on the VPS by the engineer — the agent never holds the token):**
`BIMA_NATIVE_TYPING_ENABLED`, `META_WA_PHONE_NUMBER_ID` (= `967104853150201`),
`META_WA_TOKEN` (SECRET), `META_GRAPH_VERSION` (default `v22.0`),
`BIMA_NATIVE_TYPING_KEEP_TEXT_BUBBLE`.
