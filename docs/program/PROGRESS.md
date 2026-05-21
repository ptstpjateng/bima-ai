# BIMA Program — Progress Journal

> The repo-side journal the Program Manager appends to every cycle.
> Newest entry on top. Records what *happened*; `ROADMAP.md` records
> what's *planned*; `PROGRAM-OFFICE.md` is the charter.
>
> The richer narrative history lives in the BIMA-Vault `Progress Log.md`
> (entries #1-#17). This journal starts at the Program Office handover.

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
