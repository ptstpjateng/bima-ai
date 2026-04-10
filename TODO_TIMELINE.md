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

## Phase B — Frontend Polish (Week 1–2)

### B1. Wizard flow completion
- [ ] 🔴 Verify permit wizard (`/permits/apply`) submits to `POST /api/permits` and shows success state
- [ ] 🔴 Permit detail page (`/permits/[id]`) — show status badge, documents list, next action
- [ ] 🟠 KBLI autocomplete on wizard step 1 — typeahead from backend KBLI list
- [ ] 🟠 Document upload: file input → `POST /api/permits/{id}/documents` (or S3)
- [ ] 🟡 Wizard progress stepper: visual step indicator (Step 1 of 4)

### B2. Dashboard
- [ ] 🔴 Dashboard: replace skeleton with real data — active permit count, KBLI name, next obligation
- [ ] 🟠 "Tanya BIMA-AI" chat widget: embedded chat panel that calls a `/api/ai/chat` Next.js route → forwards to ai-engine
- [ ] 🟠 License vault: list user's permits, expiry dates, download certificate button
- [ ] 🟡 LKPM reminder banner: if permit is > 3 months old and scale is Kecil/Menengah

### B3. Auth & Profile
- [ ] 🟠 Profile page: editable MSME fields (business name, KBLI, scale, NIK, NPWP)
- [ ] 🟠 Telegram link: "Connect Telegram" button → deep link to bot with `/start {token}` command
- [ ] 🟡 Magic link: show "resend link" button if user hasn't received email after 60s

### B4. UX & Design
- [ ] 🟡 Mobile responsiveness: test wizard + dashboard on 375px viewport
- [ ] 🟡 Loading skeletons on all data-fetch pages (already done on dashboard, verify permits list)
- [ ] 🟡 Empty state illustrations for zero-permit state

---

## Phase C — Backend & Admin (Week 1–2)

### C1. Telegram account linking
- [ ] 🔴 Handle `/start {token}` Telegram bot command → link `telegram_id` to user account
  - When user clicks "Connect Telegram" on portal, generate a one-time token
  - `/start TOKEN` in Telegram → backend verifies token → stores `telegram_id` on user
- [ ] 🟠 After linking, Telegram messages use user profile for personalized RAG queries

### C2. Business profile population
- [ ] 🟠 Trigger business record creation when user completes permit wizard step 1
- [ ] 🟠 `user-context` API: return `businesses` array so AI handler can personalize responses

### C3. Filament admin improvements
- [ ] 🟡 AI Interactions viewer: add full conversation thread view (group by `session_id`)
- [ ] 🟡 KBLI Scrape Targets: add "Re-scrape" action button for failed/outdated entries
- [ ] 🟡 Dashboard widgets: total messages today, active users, ChromaDB chunk count

### C4. API hardening
- [ ] 🟠 Validate `X-Internal-Key` header on all `/api/internal/*` routes (already done — verify in tests)
- [ ] 🟡 Add `POST /api/ai/chat` route on Laravel → proxies to ai-engine → used by frontend chat widget
- [ ] 🟡 Permit status webhook: when permit status changes → send Telegram notification to user

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
- [ ] 🔴 Write a 3-minute demo script covering all 3 lifecycle phases:
  1. Phase 1: "Saya mau buka warung makan, perlu izin apa?" → BIMA-AI explains KBLI 56102, NIB + Sertifikat Standar
  2. Phase 2: User opens portal → fills wizard → gets approved
  3. Phase 3: "Izin saya sudah keluar, selanjutnya apa?" → LKPM reminder, KUR info
- [ ] 🔴 Seed demo user: create `demo@bima.ai` account with complete MSME profile + 1 approved permit
- [ ] 🔴 Pre-warm model: make 1 test Telegram call 5 min before demo to avoid cold-start latency

### E2. Slide deck
- [ ] 🟠 Problem statement: UMKM licensing pain points, OSS RBA complexity
- [ ] 🟠 Solution architecture diagram (use the one in PROJECT_STATUS.md)
- [ ] 🟠 Live demo screenshots / recording backup (in case VPS is unreachable during demo)
- [ ] 🟡 Impact metrics: 35 KBLI codes indexed, 274 regulation chunks, <30s response time
- [ ] 🟡 Roadmap slide: WhatsApp, React Native, 500+ KBLI coverage

### E3. Infrastructure reliability
- [ ] 🟠 Docker Compose restart policy: confirm all services have `restart: unless-stopped`
- [ ] 🟠 Run full smoke test 1 day before demo: Telegram, portal login, permit wizard, Filament admin
- [ ] 🟡 Nginx rate limiting: prevent demo from being disrupted by stray requests
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
