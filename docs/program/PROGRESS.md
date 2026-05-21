# BIMA Program — Progress Journal

> The repo-side journal the Program Manager appends to every cycle.
> Newest entry on top. Records what *happened*; `ROADMAP.md` records
> what's *planned*; `PROGRAM-OFFICE.md` is the charter.
>
> The richer narrative history lives in the BIMA-Vault `Progress Log.md`
> (entries #1-#17). This journal starts at the Program Office handover.

---

## Cycle 8 — 2026-05-21 · "ROADMAP COMPLETE — signature-assistant, 16/16"

**Type:** PM cycle, run locally. The final cycle.

**THINK.** 15/16 reqs done. The last — #13, Head-of-DPMPTSP signing.
User decision: BIMA does NOT do cryptographic signing; it gives the
Head decision support + a deep-link into SIAP to sign there.

**DELEGATE → BIMA Team** (`bima-ai#59`): the signature-assistant.
- A `mode="signature"` variant of `OfficerCopilot` — same agent, a
  signing-framed system prompt, a read-only tool subset. Reuses
  `get_case_log_notes` / `get_validation_summary` / `get_case_full` /
  `cite_regulation` to synthesise the whole approval chain for the
  Head's signing decision. Deliberately drops `forward_case` /
  `record_decision` — the only accountable action here is the
  signature, and that happens in SIAP.
- New read tool `get_siap_signing_link` → deep-link
  `<SIAP_BASE>/admin/tanda-tangan-berkas?tableSearch=<ticket>`
  (found in SIAP's `TandaTanganBerkasResource`).
- `/case/[ticket]` gets a copilot mode toggle (Validasi / Tanda
  Tangan); signature mode shows a `SigningHandoffCard` with the
  "Tanda tangani di SIAP Jateng →" CTA. Per-mode `copilot_session`
  rows (migration `003` adds a `mode` column).
- lint + build pass, py_compile clean.

**REVIEW.** Read-only mode-scoping verified, deep-link correct,
migration consistent. Passes.

**REPORT.** `#59` merged + deployed. Migration `003` applied
(`copilot_session.mode` confirmed). ai-engine + admin-api `/health`
200. `SIAP_SIGNING_URL_BASE` defaults to Beta-SIAP.

### 🎯 ROADMAP COMPLETE
All four waves shipped. **All 16 BIMA Vision requirements addressed
and deployed on Beta-SIAP.** The vision↔shipped gap is closed.

Deferred (reported honestly, not hidden): document-upload over
WhatsApp; per-citizen `profile_id` from NIK; auto per-officer desk
scoping; the `bima_readonly` Postgres role; a SIAP role-claim on the
JWT; production-SIAP promotion (gated); the SIAP security backlog;
DB-password rotation; demo rehearsal + backup video.

### The autonomous Program Office — session tally
8 PM cycles in one session: §22 capability model → Program Office set
up → 4 roadmap waves. ~16 PRs across `ptstpjateng/bima-ai` +
`ptstpjateng/SIAP`. Deploy key, 4 scoped Sanctum tokens, 3 Alembic
migrations, the transparency poller + guided submission armed. The
daily PM Briefing routine continues at 06:00 WIB.

---

## Cycle 7 — 2026-05-21 · "Wave 4 begins — multi-ticket officer inbox"

**Type:** PM cycle, run locally.

**THINK.** Waves 1-3 done (12/16 reqs). Wave 4 is the final wave —
multi-ticket inbox (#14), SOP-as-motivator (#15), signing (#13).
#14+#15 are buildable now; #13 has a BSRE dependency.

**DELEGATE → BIMA Team** (`bima-ai#57`): the officer inbox.
- `admin-api` `GET /inbox` — JWT-gated, one parameterised SELECT on
  `ptsp.vi_monitoring_berkas_v3` ⋈ `ptsp.license` (for SOP days).
  Urgency ranker: `days_open ÷ sop_days`, bounded over-SOP boost.
  Officer→cases: returns all active cases + an optional `?desk=`
  filter — auto per-officer desk needs a SIAP `users.siap_role_id`
  (flagged for the SIAP team).
- `admin` `/inbox` page — urgency-sorted case list, click-through to
  `/case/[ticket]`.
- **SOP-as-motivator (#15):** every row leads with a calm
  days-used/SOP progress bar + encouraging Indonesian copy ("3 dari
  14 hari — masih on track"); a past-SOP case shows steady amber +
  supportive copy ("mari kita tuntaskan") — deliberately never a
  red-shame. Brand tokens, No-Line Rule, mobile-stacked.
- `npm run lint` + `npm run build` pass; `py_compile` clean.

**REVIEW.** JWT-gated, parameterised, reuses the existing SIAP read
engine (no new env var). Passes.

**REPORT.** `#57` merged + deployed. `/inbox` verified — 401 unauth
(route live, gated), admin-api `/health` 200. The `/inbox` page
auto-deploys via Vercel.

### Roadmap movement
- Wave 4: #14 + #15 ✅ done. **15 of 16 requirements complete.**
- Req #13 remaining — see plan below.

### Req #13 (signing) — the plan
Split: (a) the **signature-assistant copilot** — Head of DPMPTSP gets
a chat partner with the full approval-chain context — is buildable by
the BIMA Team; (b) the **BSRE cryptographic SK signature** is a real
SIAP/DPMPTSP-team dependency (regulated, certificate-backed — cannot
be agent-built). Building (a) delivers ~90% of #13.

### Next cycle
Cycle 8 — the signature-assistant copilot (#13 part a). Then surface
the BSRE wiring to the real SIAP team.

---

## Cycle 6 — 2026-05-21 · "Wave 3 COMPLETE — guided submission live"

**Type:** PM cycle, run locally.

**THINK.** Waves 1+2 done. Wave 3 (req #4) is the citizen-facing
capstone — BIMA conversationally files a license end to end.

**DELEGATE → BIMA Team** (`bima-ai#55`):
- `services/guided_submission.py` — a per-user state machine
  (RESOLVING_LICENSE → COLLECTING_FIELDS → REVIEW → DONE/FAILED).
  Conservative regex intent detection (needs a filing verb + a
  licensing noun). Multi-turn state in a bounded LRU (500-cap, 6h TTL)
  — the 2-turn `_history` is far too short for a form.
- Routed via a `FAST-PATH 0` block in `ai_handler.py` (non-streaming +
  streaming) — `maybe_handle()` returns `None` for non-submission
  turns, so normal chat is untouched.
- Validator gates the submit (only a `ready` score proceeds);
  validation issues block + tell the citizen what to fix.
- `services/siap_submission_client.py` — `POST /api/v1/license-request`
  with a distinct `submission:create` token.
- 15 new tests + the 27 existing all green.

**REVIEW.** Routing is `None`-safe (normal chat untouched),
feature-flagged, py_compile clean. Passes.

**REPORT.** `#55` merged + deployed. **Armed:** `submission:create`
token minted on Beta-SIAP (VPS-side, not echoed) →
`SIAP_SUBMISSION_API_TOKEN`; `GUIDED_SUBMISSION_PROFILE_ID=40766`
(MAESAROH, a Beta-SIAP test profile); `GUIDED_SUBMISSION_DEMO_FIXTURE=clean`;
`BIMA_GUIDED_SUBMISSION_ENABLED=true`. Smoke test: `maybe_handle("saya
mau ajukan izin pemakaian tanah")` engaged the flow and returned the
license-selection prompt. **Wave 3 live — req #4 done.**

### Roadmap movement
- Wave 3: ✅ COMPLETE + armed. Wave 4 (final) is next.

### Deferred (reported, not hidden)
- Document upload over the APTANA media webhook — validator runs the
  fixture path for now (real validate→branch→submit logic).
- Per-citizen `profile_id` from NIK — pinned to test profile 40766.

### Next cycle
Wave 4 — the final wave: multi-ticket inbox (#14), SOP-as-motivator
(#15), Head-of-DPMPTSP signing (#13, BSRE — likely a real-team dep).

---

## Cycle 5 — 2026-05-21 · "Transparency poller ARMED + live; officer-copilot write actions begin"

**Type:** PM cycle, run locally.

**ARMED the poller (completing cycle 4).** The `bima-service` SIAP
account was created (user-run tinker — account creation is a prohibited
agent action; the user executed the snippet). An `events:read` Sanctum
token was minted against it via `php artisan bima:issue-token`
(VPS-side, never echoed to any transcript), wired to
`SIAP_EVENTS_API_TOKEN`, and `BIMA_TRANSPARENCY_POLLER_ENABLED` flipped
on. ai-engine logs confirm: poller loop running, `GET
/license-request/changes` → HTTP 200 (token authenticates), cold-start
guard fired (50 backlog changes, 0 notified, cursor fast-forwarded).
**Vision req #12 — transparency notifications — DONE.**

**DELEGATE → BIMA Team (cycle 5 work):** the Wave 2 final slice —
officer-copilot write actions. The officer copilot gains `forward` and
`decision` actions that call SIAP's Wave-1 write endpoints, carrying
the officer's notes as context. Officer-in-the-loop: the copilot
drafts/proposes; the officer confirms; only then does BIMA execute the
write. Chained context (req 11) comes free — the notes land in SIAP's
`license_log`, so the next desk's copilot reads the prior notes.

### Roadmap movement
- Wave 2 transparency-notification deliverable: ✅ LIVE (req 12 done).
- Wave 2 officer-copilot write actions: 🔄 in progress.

### Cycle 5 outcome — WAVE 2 COMPLETE
- `bima-ai#53` (officer-copilot write actions) merged + deployed.
  Two new copilot tools — `forward_case`, `record_decision` — call
  SIAP's live write endpoints. **Officer-in-the-loop guard is
  structural:** `confirmed=False` default, the draft path is the only
  default, no code path writes without `confirmed is True`; plus a
  system-prompt rule and a per-write audit log. `get_case_log_notes`
  surfaces prior-desk notes (chained context, req 11). 27 tests green.
- Second scoped token (`workflow:advance` + `decision:draft`) minted
  on Beta-SIAP, wired to `SIAP_WRITE_API_TOKEN` (VPS-side, not echoed).
- Deployed + verified: officer copilot + write client load, token
  present, ai-engine `/health` 200.
- **Wave 2 COMPLETE.** Citizen↔officer workflow loop runs end-to-end
  on Beta-SIAP. Reqs closed across Waves 1+2: 7, 8, 10, 11, 12.

### Next cycle
Wave 3 — Guided Submission (req 4): conversational form-fill →
validator gate → submit via SIAP's create endpoint.

---

## Cycle 4 — 2026-05-21 · "Wave 2 begins — transparency-notification poller"

**Type:** PM cycle, run locally.

**THINK.** Wave 1 complete — SIAP has a write seam incl. the
`/license-request/changes` event feed. BIMA's notification dispatcher
is built + armed. Wave 2's first slice connects them so citizens get
proactive WhatsApp updates as their request moves (Vision req #12).

**PLAN.** Delegate to the BIMA Team: a background poller that consumes
the SIAP changes feed and fires citizen notifications.

**DELEGATE → BIMA Team** (`ptstpjateng/bima-ai#50`):
- `ai-engine/services/transparency_poller.py` — an asyncio background
  task (default 60s) started in the FastAPI `lifespan`, mirroring the
  admin-api reconciler pattern.
- Polls SIAP `GET /api/v1/license-request/changes` with a cursor
  persisted atomically to a JSON file (survives restart).
- SIAP status → event mapping (conservative; ambiguous changes
  skipped): rejected/perbaikan → `citizen_needs_fix`; terminal success
  + no further step → `citizen_completed`; advanced to a new step →
  `citizen_progress`.
- **N1 security:** `recipient_phone` derived server-side only —
  change `profile_id` → parameterised SELECT on `ptsp.person_profile`
  → mobile. Never from the change payload. No phone → log + skip.
- `dedupe_key = "{ticket}:{log_id}"` — no double-sends.
- Feature-flagged `BIMA_TRANSPARENCY_POLLER_ENABLED` (default OFF).

**REVIEW.** Diff read: N1 satisfied, parameterised query, idempotent,
fail-safe loop, py_compile clean. Passes.

**REPORT.** `#50` merged + deployed to ai-engine — **dormant** (flag
OFF; a no-op every tick until armed).

### Roadmap movement
- Wave 2 deliverable "wire the notification dispatcher to real
  triggers": built, merged, deployed dormant — pending arming.

### Blockers / for the user — ARMING STEP
The poller is built but dormant. To arm it (3 steps):
1. Mint a SIAP Sanctum token with the `events:read` ability on
   Beta-SIAP via `php artisan bima:issue-token` — a credential; must
   not leak to any transcript. Needs a SIAP user to attach it to
   (ideally a dedicated `bima-service` account).
2. Set `SIAP_EVENTS_API_TOKEN` in `ai-engine/.env` on the VPS.
3. Flip `BIMA_TRANSPARENCY_POLLER_ENABLED=true` (and confirm
   `BIMA_NOTIFICATIONS_ENABLED=true`, already set), restart ai-engine.
Then the Integration Agent runs an end-to-end smoke.

### Note for the SIAP team
SIAP's change payload has no remaining-SLA / ETA field, so
`citizen_progress` notifications send `eta_days="—"`. A real estimate
would need SIAP to expose remaining-SLA in the feed — future slice.

### Next cycle
Arm the poller (above), then Wave 2 continues: the workflow
orchestrator + forward-with-context in the officer copilot.

---

## Cycle 3 — 2026-05-21 · "Wave 1 COMPLETE — state events + scoped tokens"

**Type:** PM cycle, run locally.

**THINK.** Wave 1's three write endpoints were merged + live. Remaining:
the state-event feed + scoped/expiring tokens.

**PLAN.** Delegate the Wave 1 final slice to the SIAP Team.

**DELEGATE → SIAP Team** (`ptstpjateng/SIAP#23`):
- **`GET /api/v1/license-request/changes`** — `changed-since` event
  feed over `ptsp.license_log`. Cursor is `?since=<log_id>` (the
  monotonic, gap-free log sequence — chosen over a timestamp cursor
  because `created_on` can collide). Returns change rows joined to
  `license_request` + `license_approval_step`; `limit` 1-500.
  Scoped to a new `events:read` ability.
- **Scoped + expiring Sanctum tokens** — `config/sanctum.php`
  `expiration` set to 90 days (was `null` — closes audit S1; note:
  this is the effective lifetime of ALL SIAP Sanctum tokens now,
  including officer login tokens — a security improvement). New
  `php artisan bima:issue-token` command mints a scoped token
  (abilities: submission:create / workflow:advance / decision:draft /
  events:read), refuses wildcard.
- 9 new feature tests; full `Api/V1` suite 29 passing, 143 assertions.

**REVIEW.** Diff read: parameterised query builder, validated, scoped
ability, no raw SQL. Passes.

**REPORT.** `SIAP#23` merged → `main`. Deployed to Beta-SIAP via the
now-working **deploy-key `git pull`** flow (the VPS blocks outbound
port 22; the SIAP remote was switched to SSH-over-443 — `git pull`
authenticates with the read-only deploy key, the rsync band-aid is
retired).

### Roadmap movement
- **Wave 1: ✅ COMPLETE.** All 5 endpoint/token slices merged + live on
  Beta-SIAP. Only deferred item: the `bima_readonly` Postgres role
  (B1) — a DB-admin task.
- **Wave 2 unblocked** — workflow + transparency can now begin.

### Infra
- VPS→private-SIAP-repo deploy key now working over SSH port 443.
  Future SIAP deploys: `git pull` + `docker compose up -d --build
  siap-beta`. rsync retired.

### Next cycle
Wave 2, BIMA Team — the transparency-notification poller: consume
SIAP's new `/license-request/changes` feed, detect stage advances,
fire `notify(citizen_progress)` to the citizen. Connects the SIAP
event feed (#23) to the notification dispatcher (already armed) —
makes req 12 real.

---

## Cycle 2 — 2026-05-21 · "Wave 1 write seam — all 3 endpoints live on Beta-SIAP"

**Type:** PM cycle, run locally.

**THINK.** Cycle 1 shipped `POST /api/v1/license-request` (PR #21). The
remaining Wave 1 endpoints — `forward` + `decision` — were designed but
unbuilt. The SIAP repo branching was tangled: `main` had the write seam
but not the security fixes; `security/fix-batch-2026-05-18` had the
fixes but not the seam; Beta-SIAP ran the security branch. Neither had
both.

**PLAN.** Consolidate: merge the security batch into `main` so `main` is
one integrated trunk, then build slice 2 (`forward` + `decision`) and
deploy the full seam to Beta-SIAP.

**DELEGATE → SIAP Team** (note: the first slice-2 agent crashed on an
infrastructure socket error; re-run completed cleanly):
- **`POST /api/v1/license-request/{id}/forward`** — advance to
  `sort_order+1`, `APPROVED` log at the current desk. Ability
  `workflow:advance`.
- **`POST /api/v1/license-request/{id}/decision`** — record
  APPROVED/REJECTED + notes. Per user decision, a **rejection routes
  back to the previous desk** (`sort_order−1`), matching SIAP's own
  `tolak` action — not the entry step.
- Mirrors SIAP's `teruskan`/`tolak` from `EditDaftarPermohonan`. 14
  new feature tests; full `Api/V1` suite 20 passing, 89 assertions.

**REVIEW.** Diffs read: transactional, explicit `ptsp.*` query-builder
writes, validated, scoped abilities. Passes (#22). Reject-routing fix
verified — all 20 API tests green.

**REPORT.**
- **`ptstpjateng/SIAP#20`** (security batch) merged to `main` — user
  explicitly authorised reversing the earlier "leave unmerged".
- **`ptstpjateng/SIAP#21`** (create) + **`#22`** (forward + decision)
  merged. `main` is now the integrated trunk: security fixes + the
  full 3-endpoint write seam.
- Deployed to Beta-SIAP (via rsync — the VPS can't `git pull` the
  private SIAP repo yet; a deploy key is being set up). All three
  endpoints verified live (HTTP 401 = route exists, auth required);
  Beta-SIAP `/admin/login` 200 (security fixes intact).

### Roadmap movement
- Wave 1: all three write-seam endpoints ⚪/🔄 → ✅ (merged + live on
  Beta-SIAP). Wave 1 remaining: state-change events + scoped/expiring
  Sanctum tokens.

### Blockers / for the user
- **Deploy key** for the VPS→private-SIAP-repo `git pull` is generated
  and awaiting the user adding the public key to GitHub (read-only).
  Until then, SIAP deploys use rsync.
- Production SIAP promotion remains gated — Beta-SIAP only so far.
- Branch-chain irregularity: 26 of 385 Beta-SIAP licences have
  non-linear approval chains; `forward` returns 409 rather than
  guessing — a real-SIAP-team data-cleanup item.

### Next cycle
SIAP Team — Wave 1 final slice: the `changed-since` state-events
endpoint + scoped/expiring Sanctum token issuance (closes audit S1).
Then Wave 1 is complete and Wave 2 (workflow + transparency) unblocks.

---

## Cycle 1 — 2026-05-21 · "Wave 1 begins — SIAP write seam, first endpoint"

**Type:** PM cycle, run locally (the remote routine's first run produced
no output — a remote cloud agent cannot push code / open PRs; see the
note at the bottom of this entry).

**THINK.** State at cycle start: `origin/main` at #46 (Program Office
established). Roadmap Wave 1 (the SIAP write seam) not started — it is
the root blocker: everything BIMA does to SIAP is read-only today.

**PLAN.** Delegate Wave 1, slice 1 to the SIAP Team: study how SIAP
itself creates a license request, write the 3-endpoint write-seam
design doc, and build the first endpoint.

**DELEGATE → SIAP Team.** Delivered:
- **`POST /api/v1/license-request`** — `LicenseRequestController@store`.
  Validates `license_id` / `profile_id` (existence-checked) +
  `description`; resolves the licence approval chain; one DB
  transaction; returns `201 {request_id, ticket, approval_step_id,
  status}`. Errors: 422 / 409 (no approval chain) / 403 / 401.
- **Feature test** `LicenseRequestStoreTest` — 6 tests passing, wrapped
  in `DatabaseTransactions` so Beta-SIAP stays clean.
- **Design doc** `docs/bima-write-seam.md` — the full 3-endpoint
  contract (create / forward / decision) + a `changed-since` polling
  endpoint over `license_log` for state events (preferred over
  outbound webhooks).
- Mirrors SIAP's own `UserController::ajukanizin()` creation flow:
  entry step → advance to `sort_order=1` → insert `license_request` →
  stamp 9-digit `ticket` → append a `SUBMITTED` `license_log` row.
  Explicit `ptsp.*` query-builder writes — deliberately NOT
  mass-assigned (SIAP's global `Model::unguard()` is security item C1).

**REVIEW.** Diff read: explicit column arrays, parameterised query
builder, atomic transaction, input validation, scoped Sanctum ability.
No mass-assignment, no raw-SQL injection surface. Passes.

**REPORT.** PR open: **`ptstpjateng/SIAP#21`** (`feat/bima-write-seam-create`,
562 insertions, 5 files) — not merged, production untouched.

### Roadmap movement
- Wave 1 deliverable `POST /api/v1/license-request`: ⚪ → 🔄 (PR #21
  open, reviewed, awaiting human merge).

### Blockers / for the user
- **Remote routine can't push.** The autonomous remote PM run produced
  nothing in 70+ min — a remote cloud agent has no Git write
  credentials. Recommendation: reshape the routine into a daily
  *planner/reporter* (it can read + analyse + report in the run log),
  and run executing PM cycles locally — as this cycle was.
- `form_value` (SIAP's dynamic per-form payload) is deferred — a
  BIMA-created request lands at the first desk with an empty form
  payload (a valid, audited state). Populating it needs SIAP's
  dynamic-form field contract — follow-up slice.
- Scoped-token issuance with finite expiry is slice 5 (this PR wires
  the ability *check* only).

### Next cycle
SIAP Team — slice 2: the `forward` + `decision` endpoints per the
design doc. Then slice 5: scoped/expiring Sanctum tokens (closes S1).

---

## Cycle 0 — 2026-05-21 · "Program Office established"

**Type:** setup (human session, not an autonomous cycle).

**What happened:**
- The standing operating structure was created: an autonomous Program
  Manager, three dedicated teams (BIMA / SIAP / Integration), the
  THINK-FIRST protocol, and the guardrails — see `PROGRAM-OFFICE.md`.
- The gap-closing development plan was written — see `ROADMAP.md`.
  Direction: vision-gap features first; Wave 1 is the SIAP write seam.
- A daily remote routine ("BIMA PM Cycle", 06:00 WIB) was scheduled to
  fire the PM loop.

**State at handover (from BIMA-Vault Progress Log #17):**
- BIMA Vision: reqs 1 & 6 shipped; 2/3/5/8/9/16 partial; 7 & 12
  designed; 4/10/11/13/14/15 not started.
- Decisions §22 fully implemented — the SIAP tool layer is live; BIMA
  reads real SIAP requirements/status/SLA/fee.
- Notifications armed (`BIMA_NOTIFICATIONS_ENABLED=true`); 4 of 5
  templates verified; `bimanewcase` disabled pending an APTANA fix.

**Open items carried into Wave 1:**
- The SIAP write seam (create / forward / decision endpoints + state
  events) — Wave 1, SIAP Team.
- Security backlog (C1/C2/C4, DB password rotation, S1/B1).
- `bimanewcase` template recreation.
- Demo hardening + rehearsal (jumps the queue once a demo date is set).

**Next cycle:** the first autonomous PM cycle delegates Wave 1's first
slice to the SIAP Team — design the write-seam REST contract and build
`POST /api/v1/license-request` against Beta-SIAP.

---
