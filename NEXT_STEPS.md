# BIMA-AI — Next Steps

> **Last updated:** 2026-04-25
> **Status:** Core demo loop live. Entering post-hackathon hardening.
> See also: [TODO_TIMELINE.md](./TODO_TIMELINE.md), [PROJECT_STATUS.md](./PROJECT_STATUS.md)

---

## Immediate Housekeeping

- [ ] Add root `.gitignore` (missing). Must cover: `.DS_Store`, `*.bak`, `tsconfig.tsbuildinfo`, `.env`, `.env.local`, `node_modules/`, `vendor/`, `storage/logs/*`, `public/build/`
- [ ] Remove `backend-tall/app/Providers/Filament/AdminPanelProvider.php.bak`
- [ ] Decide on `lampiran-kbli.pdf` (12MB, currently tracked) — move to LFS or drop from repo if not needed for runtime
- [ ] Rebuild claude-mem corpus after large doc changes land

---

## Sprint Backlog (6 sprints, priority order)

### S1 — Stabilization 🔴 (current)
- Audit frontend for non-Volcanic-Amber color tokens (stray slate/indigo from old Ethereal theme)
- Fix any `bg-white` / `bg-black` / default Tailwind borders that violate the No-Line Rule
- Confirm Manrope typography on every route
- Smoke-test permit wizard + detail + chat widget on Vercel preview

### S2 — AI Persistence (Redis) 🟠
- Migrate `ai_handler.py` conversation history from in-memory dict to Redis (`bima:conv:{user_id}` list, LPUSH/LTRIM to last 4 turns, 24h TTL)
- Shared across ai-engine replicas; survives restarts
- Add Redis key inspection command to `/admin` debug tools

### S3 — Observability 🟠
- Structured JSON logging in ai-engine (request_id, user_id, kbli_code, rag_hits, latency_ms)
- Prometheus `/metrics` endpoint — counters for intent_calls, rag_queries, gemma_tokens, rate_limit_rejects
- Filament dashboard widget: last-24h AI cost estimate + p95 latency

### S4 — WhatsApp Channel 🟡
- Configure Meta Cloud API token in `ai-engine/.env` (`WHATSAPP_API_TOKEN`)
- Wire `/webhook/wa` → same `ai_handler.process_message()` pipeline as Telegram
- Account linking: reuse token flow from Telegram, deep link via `wa.me`
- Status notifications: extend `PermitApplicationObserver` to dispatch WA messages

### S5 — Post-License Dashboard 🟡
- "Kewajiban Pasca-Izin" list per business (LKPM quarterly, AMDAL renewal, NPWP sync)
- Due-date reminders via Telegram/WA
- Filament resource for compliance calendar

### S6 — Mobile PWA 🟢
- Convert Next.js portal to installable PWA (manifest, service worker, offline shell)
- Push notifications via web-push (reuse FCM infra)
- Evaluate React Native shell only if PWA gaps are blocking

---

## Orchestration Rules (reminder)

- **Research = parallel** — multiple Explore agents can run at once
- **Implementation = sequential** — single writer to avoid merge conflicts
- **3+ files touched** — spawn Plan agent first
- **Verification always on a running dev server** — type-check alone is not "done"

---

## Known Risks / Watchlist

- Gemma 27b latency spikes during peak Google AI Studio hours — intent model (Gemma 4b) mitigates, but response model still occasionally >8s
- ChromaDB embedded mode — no HA; loss of `chroma_data` volume = full re-scrape (~4h)
- Telegram webhook secret rotation not yet automated
- No backup strategy for PostgreSQL `bima_ai` DB on VPS
