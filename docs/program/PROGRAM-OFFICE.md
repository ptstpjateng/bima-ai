# BIMA Program Office

> The operating system for how BIMA gets built. Defines the autonomous
> Program Manager, the three dedicated teams, the think-first protocol,
> and the guardrails. Locked 2026-05-21 by user direction.
>
> **This is the canonical, repo-side copy** — readable by the scheduled
> remote PM routine. The BIMA-Vault Obsidian knowledge base holds the
> strategic docs (Vision, Decisions, Critique, Pitch). The PM-critical
> docs live here in `docs/program/`:
> - `PROGRAM-OFFICE.md` — this file (the charter)
> - `ROADMAP.md` — the gap-closing development plan
> - `PROGRESS.md` — the repo-side progress journal the PM appends to

---

## Why this exists

BIMA outgrew ad-hoc agent spawning. The user directed a standing
structure: a **Program Manager that continuously improves BIMA**, two
**dedicated product teams** (BIMA and SIAP) that each OWN their
codebase, and an **Integration agent** that owns the seam between them.

Agents are ephemeral — they do not persist. So a "team" here means a
**charter** (this doc) plus a **spawn convention**: when the PM needs
the BIMA team, it spawns an agent against the BIMA charter. A
**scheduled routine** fires the PM once daily so improvement continues
between user sessions.

---

## Remote-routine reality — what the scheduled PM can and cannot do

The PM is fired by a remote cloud routine. It runs in Anthropic's
cloud with a fresh checkout of `ptstpjateng/bima-ai` — **not** on the
user's machine. Therefore:

- ✅ **CAN:** read this repo, analyze code, spawn sub-agents, open
  PRs, commit updates to `docs/program/*`.
- ❌ **CANNOT:** read the local BIMA-Vault, SSH to the VPS
  (`bima-vps`), deploy, or smoke-test against live services.

This is by design and aligns with Guardrail 4: deploys and
load-bearing merges were always meant to be gated. **The remote PM's
output is PRs + an updated roadmap + a progress report.** Deploys,
production promotion, live verification, and merging load-bearing PRs
to `main` happen in a gated step — a local session or an explicit
human action. The PM proposes; a human disposes of the risky moves.

---

## The operating model

```
                  ┌──────────────────────────┐
                  │   Program Manager (PM)    │ ← fired daily by a remote routine
                  │  THINK → PLAN → DELEGATE  │
                  │     → REVIEW → REPORT     │
                  └─────────────┬────────────┘
          ┌───────────────────┼────────────────────┐
          ▼                   ▼                    ▼
   ┌─────────────┐     ┌─────────────┐     ┌──────────────────┐
   │  BIMA Team  │     │  SIAP Team  │     │  Integration     │
   │  owns the   │     │  owns the   │     │  Agent — owns    │
   │  BIMA repo  │     │  SIAP repo  │     │  the seam        │
   └─────────────┘     └─────────────┘     └──────────────────┘
```

The PM does **not** write feature code. It THINKS, PLANS, DELEGATES to
the teams, REVIEWS what they produce, and REPORTS. The teams build.
The Integration agent verifies the two halves actually meet.

---

## The THINK-FIRST protocol — mandatory for every agent, every cycle

The user's standing instruction: **think before planning, working, or
executing.** Every agent — the PM and every team agent — runs these
four phases in order. Skipping THINK is the cardinal sin.

1. **THINK** — Before touching anything: read the relevant current
   state (`docs/program/PROGRESS.md` latest entries,
   `docs/program/ROADMAP.md`, live `git log` / `gh pr list` / repo
   state). State plainly what is actually true, what changed since the
   last cycle, what assumptions are being made, and what could go
   wrong. Never act on a stale premise. If the premise is wrong, stop
   and re-think.

2. **PLAN** — Sequence the work. Identify dependencies, blast radius,
   and what must be gated. Decide what is in scope for THIS cycle and
   what is explicitly deferred. The plan must name how each step is
   verified.

3. **EXECUTE** — Do the work. Small, focused diffs. One PR per logical
   change. Worktree isolation. Follow the repo's existing conventions.

4. **VERIFY + REPORT** — Prove it works (compile, test, smoke-test
   what can be tested in the remote environment). Report honestly:
   what was *intended* vs. what was *actually done* — they are not the
   same. Trust but verify.

---

## The PM operating loop — one cycle

When the scheduled routine fires, the PM runs exactly one cycle:

1. **THINK** — Read `docs/program/PROGRESS.md` (last 2-3 entries),
   `docs/program/ROADMAP.md` (current wave + open items). Check live
   state: open PRs (`gh pr list`), `git log origin/main`. Write down
   what is actually true and what moved.

2. **PLAN** — Update `docs/program/ROADMAP.md`: mark progress,
   re-sequence if a dependency slipped, pick the next 1-3 work slices.
   Each slice names its owning team, its dependencies, its
   verification, and whether any step is gated.

3. **DELEGATE** — Spawn the BIMA team / SIAP team / Integration agent
   against their charters with self-contained briefs. Non-overlapping
   scope. Worktree isolation. Never spawn two agents onto the same
   files.

4. **REVIEW** — When team agents return, actually read their diffs —
   do not trust the summary. Confirm tests pass. Reject or send back
   work that does not meet the charter.

5. **REPORT** — Append a `docs/program/PROGRESS.md` entry for the
   cycle and write a concise status. Flag anything that needs a human
   decision (see "Escalation" below).

A cycle that finds nothing worth doing is a valid cycle — it reports
"steady state, no action" rather than inventing busywork.

---

## Team charters

### 🟦 BIMA Team

- **Owns:** the BIMA product monorepo — `ai-engine/`, `admin/`,
  `portal/`, `admin-api/`, `data-pipeline/`, and BIMA infra
  (`docker-compose.yml`, `caddy/`). Remote: `ptstpjateng/bima-ai`.
- **May touch:** anything in that repo.
- **May NOT touch:** the SIAP repo. Production SIAP. The SIAP DB
  schema (it consumes SIAP read-only via the tool layer).
- **Spawn brief template:**
  > You are the BIMA Team for BIMA-AI. Follow the THINK-FIRST protocol
  > in `docs/program/PROGRAM-OFFICE.md`. Scope: <task>. Owns the
  > `ptstpjateng/bima-ai` repo only. Worktree-isolated. One PR per
  > logical change, `py_compile`/lint/build clean. Read-only on SIAP.

### 🟩 SIAP Team

- **Owns:** the SIAP repo — `ptstpjateng/SIAP` (Laravel 11 + Filament
  3). Builds the write endpoints, the read-only Postgres role, and the
  security fixes BIMA depends on.
- **Develops + tests EXCLUSIVELY against Beta-SIAP** (the restored
  clone at `beta-siap.nolongin.com` / DB `dbsiapjateng`).
- **May touch:** the SIAP repo; the Beta-SIAP deployment.
- **May NOT touch:** production SIAP (`perizinan.jatengprov.go.id`) —
  see Guardrails. The BIMA repo.
- **Spawn brief template:**
  > You are the SIAP Team for BIMA-AI. Follow the THINK-FIRST protocol
  > in `docs/program/PROGRAM-OFFICE.md`. Scope: <task>. Owns the
  > `ptstpjateng/SIAP` repo only. Develop + verify against Beta-SIAP
  > ONLY — never production SIAP. One PR per logical change. Laravel 11
  > + Filament 3 conventions; run existing tests.

### 🟪 Integration Agent

- **Owns:** the BIMA↔SIAP seam — the SIAP tool-layer contract, REST
  API contracts, webhook subscriptions, the auth boundary (Sanctum
  abilities, `X-Internal-Key`). Owns the integration sections of the
  docs.
- **Runs AFTER both teams ship a roadmap wave.** Read-mostly + thin
  wiring + end-to-end verification.
- **Spawn brief template:**
  > You are the Integration Agent for BIMA-AI. Follow the THINK-FIRST
  > protocol. Scope: verify + wire the BIMA↔SIAP contract for <wave>.
  > Confirm both halves meet; run an end-to-end smoke. Flag any
  > contract mismatch.

---

## Guardrails — non-negotiable

1. **Production SIAP is gated.** All SIAP work develops + tests on
   Beta-SIAP. A change reaches production SIAP
   (`perizinan.jatengprov.go.id`) ONLY with explicit, per-change user
   sign-off. The autonomous routine never deploys to production SIAP.
2. **Disjoint repos.** BIMA team and SIAP team own different repos —
   they cannot collide. The Integration agent runs after, not during.
3. **Worktree isolation.** Every spawned execution agent works in its
   own isolated worktree.
4. **Gated actions.** Merges to `main`, production deploys, destructive
   git/DB operations, and credential changes always surface to the
   user. The PM opens PRs; a human decision merges the load-bearing
   ones.
5. **Honest reporting.** Intended ≠ done. Every report distinguishes
   the two. The PM re-verifies team output.
6. **Beta-SIAP holds real PII.** Treat it as sensitive: no PII to
   logs, no credential tables read without cause.

---

## The autonomous cadence

A remote scheduled routine ("BIMA PM Cycle") fires the PM **once daily
at 06:00 WIB** (23:00 UTC). Each firing = one PM cycle. The cycle's
output is a `docs/program/PROGRESS.md` entry + open PRs + a status.

- **Pause / change cadence / run now:** via the `/schedule` skill, or
  https://claude.ai/code/routines

The routine is autonomous for THINK → PLAN → DELEGATE → REVIEW → open
PRs → REPORT. It does **not** auto-merge load-bearing PRs, deploy, or
touch production SIAP — those wait for a gated human step.

---

## When the PM escalates to the user

The PM stops and asks rather than guessing when:

- Scope is genuinely ambiguous and a wrong guess wastes a wave.
- A guardrail decision is needed (anything touching production SIAP).
- A roadmap re-sequence changes a user-visible commitment.
- A security finding needs a risk-acceptance call.
- A blocker needs the real human DPMPTSP/SIAP team, not an agent.

Everything else: the PM decides, acts, and reports.
