# BIMA Roadmap — closing the vision↔shipped gap

> The development plan that takes BIMA from what's shipped today to the
> end-state vision. Maintained by the Program Manager — see
> `PROGRAM-OFFICE.md`. The PM updates this doc every cycle: marks
> progress, re-sequences on dependency slips, picks the next slices.
>
> Direction locked 2026-05-21: **vision-gap features first.** Build the
> unbuilt requirements; security + demo-hardening run as cross-cutting
> tracks alongside, not ahead.
>
> The 16 requirements + the four-phase vision live in the BIMA-Vault
> (`BIMA Vision.md`). The distilled state the PM needs is below.

---

## Current state (2026-05-21, after Decisions §22)

Of the 16 BIMA Vision requirements:

| Status | Reqs | Notes |
|---|---|---|
| ✅ shipped | 1, 6 | KBLI chat; the completion validator (killer feature) |
| 🟢 partial / working | 2, 3, 5, 8, 9, 16 | scope-aware intent; **SIAP tool layer** (req 3 now deep); 2-turn memory; officer copilot v2; admin UX; 5-agent runtime |
| 🟡 designed, not wired | 7, 12 | notification dispatcher built + armed — needs real workflow triggers |
| ⚪ not started | 4, 10, 11, 13, 14, 15 | guided submission; forward + chained context; signing; multi-ticket inbox; SOP gamification |

The 16 reqs (verbatim): 1 KBLI/pre-license chat · 2 intent routing ·
3 SIAP license context · 4 guided submission · 5 user-context memory ·
6 completion-percentage validation · 7 next-flow ping · 8 officer chat
+ doc Q&A + suggested score · 9 officer "AHA" UX · 10 forward to next
flow · 11 chained context across flows · 12 transparency notifications ·
13 Head-of-DPMPTSP signing · 14 multi-ticket handling · 15 SOP-as-
motivator · 16 multi-agent architecture.

**The gap is the ⚪ + 🟡 reqs — and they share one root blocker.**

---

## The critical-path insight

Everything BIMA does to SIAP today is **read-only**. Reqs 4, 7, 10, 11,
12, 13 all require BIMA (or an officer through BIMA) to **write** to
SIAP — create a request, advance an approval step, record a decision,
fire a state change. SIAP exposes no write API today.

So the roadmap's first wave is **the write seam**. Until it exists,
the workflow reqs cannot truly land. The SIAP Team builds it — on
Beta-SIAP — and the Integration Agent defines the contract BIMA
consumes.

```
Wave 1 (write seam) ──► Wave 2 (workflow + notifications)
        │                       │
        └──► Wave 3 (guided submission)
                                │
                        Wave 4 (multi-ticket + signing)
```

---

## Wave 1 — The Write Seam   ·   status: NOT STARTED

**Goal:** SIAP can be written to, safely and contractually. **Owner:**
SIAP Team builds; Integration Agent defines the contract. **All on
Beta-SIAP.**

| Deliverable | Closes / unblocks | Status |
|---|---|---|
| `POST /api/v1/license-request` — create a new license request | req 4 | ⚪ |
| `POST /api/v1/license-request/{id}/forward` — advance the approval step | reqs 7, 10 | ⚪ |
| `POST /api/v1/license-request/{id}/decision` — record an officer's decision + notes | reqs 8, 11 | ⚪ |
| State-change events — webhook BIMA subscribes to, OR a `changed-since` polling endpoint | reqs 7, 12 | ⚪ |
| Scoped, expiring Sanctum tokens (per-endpoint abilities) + a read-only Postgres role | audit S1, B1 | ⚪ |

**Dependencies:** none — SIAP Team can start immediately on Beta-SIAP.
**Verification:** Integration Agent runs an end-to-end create→forward→
decision cycle against Beta-SIAP and confirms the REST contract.
**Production gate:** these endpoints reach production SIAP only with
explicit per-change user sign-off.

---

## Wave 2 — Workflow + Transparency   ·   status: BLOCKED ON WAVE 1

**Goal:** a license request moves through SIAP's approval chain with
BIMA notifying everyone at each step. **Owner:** BIMA Team. **Needs:**
Wave 1's forward endpoint + state events.

| Deliverable | Closes | Status |
|---|---|---|
| Workflow orchestrator — state machine over SIAP's `license_approval_step` chain | 7, 16 | ⚪ |
| Wire the notification dispatcher to real triggers: `new_case` on forward, `citizen_progress` on stage change, `sla_warn` on threshold | 7, 12 | ⚪ |
| Forward-with-context — officer copilot gains a "forward to next stage" action carrying prior notes | 10, 11 | ⚪ |

---

## Wave 3 — Guided Submission   ·   status: BLOCKED ON WAVE 1

**Goal:** BIMA walks a citizen through filing a license application end
to end. **Owner:** BIMA Team. **Needs:** Wave 1's create endpoint.

| Deliverable | Closes | Status |
|---|---|---|
| Conversational form-fill — collect required fields + documents over WhatsApp / portal, license-specific (uses `siap_get_requirements`) | 4 | ⚪ |
| Pre-submit validation gate — runs the existing validator (req 6); only a clean score submits | 4 | ⚪ |
| Submit via the SIAP create endpoint; return the citizen their ticket | 4 | ⚪ |

---

## Wave 4 — Multi-ticket + Signing   ·   status: BLOCKED ON WAVE 1-2

**Goal:** officers handle volume; the Head of DPMPTSP signs. **Owner:**
BIMA Team + SIAP Team. **Partly external** (BSRE digital signature).

| Deliverable | Closes | Status |
|---|---|---|
| Inbox ranker + multi-ticket admin UX — officer queue sorted by urgency | 14 | ⚪ |
| SOP-as-motivator — SLA progress surface, framed as encouragement | 15 | ⚪ |
| Head-of-DPMPTSP signing — chat-with-context + digital signature via SIAP's BSRE | 13 | ⚪ |

**Dependency flag:** req 13 (signing) needs SIAP's BSRE integration —
likely a **real-human-team dependency** the PM must surface early.

---

## Cross-cutting tracks (run alongside the waves)

Vision-gap features are the *first focus*, so these run in parallel
slices, not ahead — but the PM keeps them visible and escalates if
they become urgent.

- **Security backlog** — C1 (`Model::unguard()`), C2 (`canAccessPanel()`
  open to all), C4 (SQLi concatenation) on Beta-SIAP; DB password
  rotation (leaked to a transcript 2026-05-21). S1 + B1 fold into
  Wave 1.
- **Demo hardening** — rehearsal script, dry-runs, backup video,
  fixture verification. Flagged as the top *execution* risk. **If a
  hackathon demo date is set, demo hardening jumps ahead of the
  current wave** — the PM must enforce this.
- **`bimanewcase` template** — recreate in APTANA with correct copy,
  Meta re-approval, then un-disable the `new_case` event.

---

## The production-SIAP promotion gate

Every Wave-1/Wave-4 SIAP change is built and fully verified on
Beta-SIAP. Promoting any of it to **production SIAP**
(`perizinan.jatengprov.go.id`) is a separate, explicit, per-change step
requiring user sign-off and coordination with the human DPMPTSP/SIAP
team. Beta-SIAP gets the full end-to-end; production is a gated
promotion, never automatic.

---

## How the PM maintains this doc

Every cycle, the PM: flips deliverable status (⚪→🔄→✅), re-sequences
if a dependency slipped, and records the next 1-3 slices it delegated.
This doc is the single source of truth for "what's next";
`PROGRESS.md` records what happened.
