# BIMA-AI — Todo Checklist & Timeline

> **Project:** DPMPTSP Jawa Tengah Hackathon
> **Target event:** TBD (assume 2–3 weeks from 2026-04-10)
> **Current date:** 2026-04-10
> **Overall progress:** ~65% complete for core demo loop

---

## Priority Legend

- 🔴 **P0** — Blocks demo / core flow broken without it
- 🟠 **P1** — Must have for a convincing hackathon demo
- 🟡 **P2** — Should have — differentiates the project
- 🟢 **P3** — Nice to have / stretch goal

---

## Phase A — AI Engine Hardening (Week 1) ✅ COMPLETE

### A1. Fix `analyze_user_intent` KBLI targeting in live requests
- [x] 🔴 Verify detected KBLI is actually prefixing the RAG query — `rag_query=` now logged on every request
- [x] 🔴 Test with explicit KBLI mention: "apa persyaratan KBLI 56102?" — confirmed `kbli_code=56102` extracted
- [x] 🟠 Handle 5-digit vs 4-digit KBLI formats — normalization strips non-digits, accepts 4–6 digit codes

### A2. Expand ChromaDB knowledge base
- [x] 🟠 Scrape 25 more high-priority KBLI codes — inserted as pending, pipeline triggered 2026-04-10
  - 10110, 10120, 47221, 47810, 47811, 86109, 93199, 96011, 96012, 43210, 56104, 56209, 47191, 47111, 77390, 74100, 73100, 72190, 64110, 66221, 68100, 55130, 82910, 85101, 85102
- [x] 🟠 Pipeline triggered via `POST localhost:9000/pipeline/trigger?limit=25`
- [ ] 🟡 Add KBLI category metadata to chunks (Makanan, Kesehatan, Perdagangan, Konstruksi)
- [ ] 🟡 Chunk quality review — inspect 5 random KBLI chunks for accuracy

### A3. Response quality
- [x] 🟠 Phase 2 portal CTA enforced programmatically — appended if `phase==2` and URL missing from response
- [x] 🟠 RAG fallback tested — `_rag_fallback_response()` returns friendly message, no technical terms exposed
- [x] 🟡 `GEMINI_INTENT_MODEL` env var — set to `models/gemma-3-4b-it` for faster intent calls (independent of main model)
- [x] 🟡 Conversation history — last 2 turns per user stored in-memory, passed as `contents[]` array to API

### A4. Stability
- [x] 🟠 `requirements.txt` pinned: `huggingface-hub==0.27.0`, `transformers==4.47.0`, `sentence-transformers>=3.0,<6.0`
- [x] 🟡 Rate limiter: 5 msg/min per `user_id` — friendly Indonesian rejection, tested and verified
- [x] 🟡 `/health` enriched: returns `model`, `intent_model`, `chroma_chunks`, `chroma_status`

---

## Phase B — Frontend Polish ✅ COMPLETE

### B1. Wizard flow completion ✅
- [x] 🔴 Permit wizard submits to `POST /api/permits/apply` — shows success state with application number
- [x] 🔴 Permit detail page (`/permits/[id]`) — fetches full data from `GET /api/permits/detail/{id}`, shows status badge, next-action card, documents list, requirements checklist
- [x] 🟠 KBLI typeahead on wizard step 1 — 300ms debounce, click-outside close, auto-fills code/description/section
- [ ] 🟠 Document upload: file input → storage (skipped — demo uses pre-seeded data)
- [ ] 🟡 Wizard progress stepper (skipped — steps already labelled in header)

### B2. Dashboard ✅
- [x] 🔴 Dashboard shows real permit data via SWR — permit count, status, KBLI name
- [x] 🟠 "Tanya BIMA-AI" chat widget — collapsible, server-token-validated, starter prompts, typing indicator
- [x] 🟡 LKPM reminder banner — fires for kecil/menengah permits >90 days old

### B3. Auth & Profile ✅
- [x] 🟠 Profile edit mode — inline form for name, phone, NIK, NPWP, business name, address via `PATCH /api/profile`
- [x] 🟠 Telegram connect — generates 15-min token → deep link `https://t.me/bima_ai_bot?start=tglink_{token}`; shows linked badge if already connected

### B4. UX & Design ✅
- [x] 🟡 Mobile responsive — AppLayout uses BottomNav + `pb-24`, all grids use `sm:grid-cols-2`
- [x] 🟡 Skeletons on all data-fetch pages (dashboard, permits list, permit detail)
- [x] 🟡 EmptyState component used on dashboard zero-permit and filtered permits list

### Security hardening (added during B) ✅
- [x] CORS locked to `FRONTEND_URL` — no more wildcard `*`
- [x] `/api/ai/chat` validates Bearer token against `/api/auth/me` before forwarding; user_id is server-extracted
- [x] `GET /api/permits/detail/{id}` with MSME ownership enforcement
- [x] `throttle:120,1` on all `auth:sanctum` routes
- [x] Document path validated as URL; type restricted to enum; notes capped at 2000 chars

---

## Phase C — Backend & Admin ✅ COMPLETE

### C1. Telegram account linking ✅
- [x] 🔴 `POST /api/internal/telegram/link` — validates `tglink_{token}` (15-min TTL), binds `telegram_chat_id`, prevents duplicate linking (409)
- [x] 🔴 Bot detects `/start tglink_{TOKEN}` → calls backend link → sends confirmation or error message
- [x] 🟠 Bot `/start` (no token) → onboarding welcome message
- [x] 🟠 After linking, `user-context` API returns `telegram_chat_id` — AI can reference Telegram identity

### C2. Business profile population ✅
- [x] 🟠 `PermitController@apply` upserts primary `Business` record in same DB transaction — syncs kbli_code, scale, revenue, employee_count, location
- [x] 🟠 `UserContextController@show` already returns `businesses[]` — AI personalization fully wired

### C3. Filament admin improvements ✅
- [x] 🟡 AI Interactions table: session thread filter (filter by session_id), "view thread" action, today-only toggle, 30s live poll
- [x] 🟡 KBLI Scrape Targets: re-scrape action + bulk re-queue already present
- [x] 🟡 New `UserStatsWidget`: UMKM users, Telegram-linked count, active users (24h), pending/approved permits
- [x] 🟡 `PipelineStatsWidget` already shows messages today + ChromaDB chunks

### C4. API hardening ✅
- [x] 🟠 X-Internal-Key validated with `hash_equals` on all `/api/internal/*` routes
- [x] 🟡 Permit status → Telegram notification: `PermitApplicationObserver` sends formatted Telegram message on approved/rejected/additional_docs/under_review status changes

---

## Phase D — WhatsApp (Week 2)

- [ ] 🟠 Configure Meta App: add phone number, get permanent token, set webhook URL
- [ ] 🟠 Set `WHATSAPP_API_TOKEN` and `WHATSAPP_APP_SECRET` in `ai-engine/.env` on VPS
- [ ] 🟠 Test webhook signature validation: `X-Hub-Signature-256`
- [ ] 🟠 End-to-end test: send WhatsApp message → receive AI reply
- [ ] 🟡 WhatsApp template messages for outbound notifications (permits approved etc.)

---

## Phase E — Demo Preparation (Week 2–3)

### E1. Demo script
- [x] 🔴 Write a 3-minute demo script covering all 3 lifecycle phases — see `DEMO_SCRIPT.md`
  1. Phase 1: "Saya mau buka warung makan, perlu izin apa?" → BIMA-AI explains KBLI 56102, NIB + Sertifikat Standar
  2. Phase 2: User opens portal → fills wizard → gets approved
  3. Phase 3: "Izin saya sudah keluar, selanjutnya apa?" → LKPM reminder, KUR info
- [x] 🔴 Seed demo user: `DemoSeeder.php` creates `demo@bima.ai` + `admin@bima.ai`, business record, 1 approved + 1 under_review permit
  - Run: `php artisan db:seed --class=DemoSeeder`
- [ ] 🔴 Pre-warm model: make 1 test Telegram call 5 min before demo to avoid cold-start latency

### E2. Slide deck
- [ ] 🟠 Problem statement: UMKM licensing pain points, OSS RBA complexity
- [ ] 🟠 Solution architecture diagram (use the one in PROJECT_STATUS.md)
- [ ] 🟠 Live demo screenshots / recording backup (in case VPS is unreachable during demo)
- [ ] 🟡 Impact metrics: 35 KBLI codes indexed, 274 regulation chunks, <30s response time
- [ ] 🟡 Roadmap slide: WhatsApp, React Native, 500+ KBLI coverage

### E3. Infrastructure reliability
- [x] 🟠 Docker Compose restart policy: all long-running services have `restart: unless-stopped` (`data-pipeline` intentionally `"no"`)
- [ ] 🟠 Run full smoke test 1 day before demo: Telegram, portal login, permit wizard, Filament admin
- [x] 🟡 Nginx rate limiting: `limit_req_zone` added — 30r/m on `/webhook/`, 120r/m on `/api|admin`
- [ ] 🟡 Backup ChromaDB volume: `docker exec bima-ai-ai-engine-1 tar czf /tmp/chroma_backup.tar.gz /app/chroma_db`

---

## Phase F — Stretch Goals (Post-hackathon)

- [ ] 🟢 React Native mobile app (`/mobile` directory)
- [ ] 🟢 Expand to 200+ KBLI codes
- [ ] 🟢 Multi-turn conversation history stored in PostgreSQL
- [ ] 🟢 Proactive notifications: license expiry alerts via Telegram
- [ ] 🟢 LKPM auto-fill assistant: help user fill quarterly report
- [ ] 🟢 Document OCR: scan and parse uploaded permit documents
- [ ] 🟢 Analytics dashboard: usage metrics, most-asked KBLI codes, conversion funnel

---

## Quick Reference — Key Commands

```bash
# Deploy ai-engine code change
scp ai-engine/services/ai_handler.py bima-vps:~/bima-ai/ai-engine/services/ai_handler.py
ssh bima-vps "docker restart bima-ai-ai-engine-1"

# Check live logs
ssh bima-vps "docker logs bima-ai-ai-engine-1 -f --tail 30"

# Check all containers
ssh bima-vps "docker ps --format 'table {{.Names}}\t{{.Status}}'"

# Trigger data pipeline (scrape N more KBLI codes)
ssh bima-vps "curl -s -X POST 'http://localhost/api/pipeline/trigger?limit=10' -H 'X-Internal-Key: <key>'"

# ChromaDB chunk count
ssh bima-vps "docker exec bima-ai-ai-engine-1 python3 -c \"import chromadb; c=chromadb.PersistentClient('/app/chroma_db'); print(c.get_collection('oss_regulations').count())\""

# Git push (triggers Vercel frontend deploy)
git push origin main
```

---

## Timeline Summary

| Week | Focus | Exit Criteria |
|---|---|---|
| **Week 1** (Apr 10–17) | AI hardening, frontend wizard, Telegram account linking | Full demo loop works: Telegram → RAG → Gemma → reply + portal permit submission |
| **Week 2** (Apr 17–24) | WhatsApp, dashboard with real data, admin polish | Both channels live; dashboard shows real permit data; demo script tested |
| **Week 3** (Apr 24–30) | Demo prep, slide deck, reliability testing | 3-min demo runs clean 3× in a row without errors |
