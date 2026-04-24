# BIMA-AI — Project Status, System Architecture & Schema

> **Last updated:** 2026-04-10  
> **Hackathon:** DPMPTSP Jawa Tengah — OSS RBA AI Assistant

---

## 1. Executive Summary

BIMA-AI is a production-deployed, omnichannel AI assistant that guides Indonesian UMKM owners through the full business licensing lifecycle (OSS RBA). The core messaging pipeline (Telegram → FastAPI → Gemma → ChromaDB → Reply) is **live and working**. The Next.js frontend portal is **live on Vercel**. The Laravel admin panel is **live on VPS**.

**What works end-to-end today:**
- Telegram bot: receives messages → intent classification → ChromaDB RAG → Gemma reply in Indonesian
- Telegram account linking: portal generates 15-min token → deep link → bot detects `/start tglink_TOKEN` → backend links account → confirms to user
- Telegram notifications: permit status changes (approved/rejected/under_review/additional_docs) trigger formatted Telegram messages via `PermitApplicationObserver`
- Next.js portal: magic-link auth, permit wizard with KBLI typeahead, permit detail page (per-record API with ownership check), profile inline editing, Telegram connect flow, LKPM banner, chat widget (server-verified identity)
- Business record auto-populated on every permit application (upserted in same DB transaction)
- Filament admin: UserStatsWidget, enhanced AI Interactions with session thread view, KBLI scrape management
- 35+ KBLI codes fully scraped; pipeline running more (25 additional queued 2026-04-10)

---

## 2. Current Deployment State

| Service | Status | URL / Notes |
|---|---|---|
| **Nginx reverse proxy** | ✅ Live | `http://116.254.113.81:80` |
| **Laravel backend** (FrankenPHP) | ✅ Live | `http://116.254.113.81:8000` (direct), `/api`, `/admin` via nginx |
| **Filament Admin Panel** | ✅ Live, styled | `http://116.254.113.81/admin` |
| **FastAPI AI Engine** | ✅ Live | internal `ai-engine:8000`; exposed via `/webhook/` |
| **PostgreSQL** | ✅ Live | internal `postgres:5432` |
| **Redis** | ✅ Live | internal `redis:6379` |
| **Next.js Frontend** | ✅ Live | `https://project-5z22k.vercel.app` (auto-deploy on push to `main`) |
| **Telegram Bot** | ✅ Live | webhook configured, messages processing, account linking working |
| **WhatsApp Bot** | ⏳ Not configured | Meta token placeholder — Phase D |
| **Data Pipeline** | ✅ Running | 35 done, 25 more queued (triggered 2026-04-10) |

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        USER CHANNELS                                 │
│   Telegram Bot              WhatsApp (Meta Cloud)    Next.js Portal  │
│   @bima_ai_bot              (not yet configured)     Vercel          │
└──────┬───────────────────────────────────────────────────┬──────────┘
       │  HTTPS webhook POST                               │ HTTPS API
       ▼                                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  VPS  116.254.113.81  (Docker Compose)                               │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  nginx :80  (sole public entry)                             │    │
│  │   /webhook/*         → ai-engine:8000                       │    │
│  │   /api, /admin, /sanctum, /storage → backend:80             │    │
│  │   /                  → 302 /admin                           │    │
│  └────────────────┬──────────────────────┬──────────────────────┘   │
│                   │                      │                           │
│       ┌───────────▼──────┐   ┌───────────▼────────────────────┐    │
│       │  FastAPI AI      │   │  Laravel 13 + FrankenPHP        │    │
│       │  ai-engine:8000  │   │  backend:80                     │    │
│       │                  │   │                                 │    │
│       │  /webhook/tg     │   │  /api/auth  (magic link)        │    │
│       │  /webhook/wa     │   │  /api/auth/me                   │    │
│       │  /webhook/chat   │◄──┤  /api/permits                   │    │
│       │  /vectorize      │   │  /api/permits/detail/{id}       │    │
│       │  /health         │   │  /api/profile                   │    │
│       │                  │   │  /api/profile/telegram-token    │    │
│       │  Services:       │   │  /api/internal/ai-logs          │    │
│       │  ├ ai_handler    │──►│  /api/internal/user-context     │    │
│       │  ├ rag_service   │   │  /api/internal/telegram/link    │    │
│       │  └ user_context  │   │  /api/pipeline/trigger          │    │
│       └──────┬───────────┘   │  /admin  (Filament)             │    │
│              │               └──────────┬──────────────────────┘    │
│       ┌──────▼───────────┐  ┌───────────▼─────┐  ┌─────────────┐  │
│       │  ChromaDB        │  │  PostgreSQL 16   │  │  Redis 7    │  │
│       │  (embedded,      │  │  :5432 internal  │  │  sessions   │  │
│       │   persistent)    │  │                  │  │  queues     │  │
│       │  35+ KBLI codes  │  │  Tables:         │  │  cache      │  │
│       │  collection:     │  │  users           │  └─────────────┘  │
│       │  oss_regulations │  │  businesses      │                   │
│       └──────────────────┘  │  permit_applic.  │                   │
│                             │  ai_interactions  │                   │
│                             │  kbli_scrape_t.  │                   │
│                             │  magic_link_tok. │                   │
│                             └──────────────────┘                   │
└─────────────────────────────────────────────────────────────────────┘
               │
               │  External APIs
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Google Generative Language API                                      │
│  models/gemma-3-27b-it  (main) + models/gemma-3-4b-it (intent)     │
│  generativelanguage.googleapis.com/v1beta                           │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Vercel (Frontend)                                                   │
│  Next.js 15 — project-5z22k.vercel.app                              │
│  Auto-deploys on push to main (root: frontend/)                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Request Flow — Telegram Message

```
User sends message on Telegram
        │
        ▼
Telegram POST /webhook/telegram/  (ai-engine)
        │
        ├─ Signature validation (TELEGRAM_SECRET_TOKEN)
        │
        ├─ text = "/start tglink_{TOKEN}"?
        │       └─► handle_telegram_link(chat_id, token)
        │               ├─ POST /api/internal/telegram/link  (Laravel)
        │               └─ Sends confirmation or error message to user
        │
        ├─ text = "/start"?
        │       └─► handle_start_command(chat_id) — sends onboarding welcome
        │
        └─ Normal message → process_message()  [ai_handler.py]
                │
                ├─ Rate limit check: 5 msg/min per chat_id (in-memory)
                ├─ 1. analyze_user_intent()  ── Gemma 4b JSON call ─► phase (1/2/3) + KBLI code
                ├─ 2. fetch_user_context()  ─── GET /api/internal/user-context (Laravel)
                │       └─ business profile, license vault, Telegram↔user binding
                ├─ 3. query_regulations()   ─── ChromaDB semantic search
                │       └─ KBLI-prefixed query if KBLI detected (n=8), else n=4
                ├─ 4. Build systemInstruction (system prompt + user ctx + RAG chunks)
                ├─ 5. _call_gemma_with_retry()  ─ Google API (3 attempts, backoff 1s/2s)
                │       └─ Falls back to _rag_fallback_response() if all retries fail
                │       └─ Appends portal CTA if phase==2 and URL missing
                ├─ 6. _send_telegram_reply()  ── Telegram sendMessage (Markdown)
                └─ 7. log_to_backend()  ──────── POST /api/internal/ai-logs (fire-and-forget)
```

---

## 5. Request Flow — Next.js Portal Chat Widget

```
User types message in ChatWidget
        │
        ▼
POST /api/ai/chat  (Next.js API Route — server-side)
        │
        ├─ Read Authorization: Bearer {token} from request header
        ├─ Validate token: GET /api/auth/me  (Laravel)
        │       └─ Reject 401 if token invalid or expired
        ├─ Extract verified user_id from /auth/me response
        │       └─ Client-supplied user_id is IGNORED entirely
        │
        ▼
POST /webhook/chat  (ai-engine — via AI_ENGINE_URL env var)
        │  Body: { user_id: "web-{verifiedId}", message }
        ├─ generate_ai_response()  (same AI pipeline as Telegram)
        └─ Returns { response, elapsed }
```

---

## 6. AI Pipeline Detail

### 6.1 LLM

| Property | Value |
|---|---|
| **Main model** | `models/gemma-3-27b-it` (env: `GEMINI_MODEL`) |
| **Intent model** | `models/gemma-3-4b-it` (env: `GEMINI_INTENT_MODEL`) |
| **Hosting** | Google AI Studio (same infra as Gemini) |
| **No GPU on VPS** | ✅ — all inference is remote |
| **thinkingConfig** | Not sent (not supported by Gemma) |
| **JSON output** | Enforced via prompt only; strip Markdown fences before `json.loads()` |
| **Timeout** | 120s per call |
| **Max output tokens** | 2048 (main), 128 (intent) |
| **Retry** | 3 attempts, 1s / 2s backoff on 429/503 |
| **Fallback** | RAG-only response if all retries fail |

### 6.2 Conversation History

- Last 2 turns per user stored in-memory (keyed by `user_id`)
- Passed as `contents[]` array to the Gemma API alongside the system instruction
- Separate history per channel (Telegram, WhatsApp, web)

### 6.3 Embeddings (RAG)

| Property | Value |
|---|---|
| **Model** | `paraphrase-multilingual-MiniLM-L12-v2` |
| **Dimensions** | 384 |
| **Normalisation** | L2 (cosine distance in ChromaDB) |
| **Used in** | Both ingest (data-pipeline) and query (rag_service) |
| **Loaded as** | Module-level singleton (`_get_embedder()`) |

### 6.4 ChromaDB

| Property | Value |
|---|---|
| **Collection** | `oss_regulations` |
| **Distance metric** | cosine (`hnsw:space=cosine`) |
| **KBLI codes indexed** | 35+ (pipeline running more) |
| **Persistence** | Docker volume `chroma_data` at `/app/chroma_db` |

---

## 7. Database Schema (PostgreSQL)

### `users`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `name` | varchar | |
| `email` | varchar unique | |
| `password` | varchar | hashed (cast) |
| `role` | varchar | `msme` / `admin` |
| `telegram_chat_id` | bigint nullable | links Telegram chat to account |
| `telegram_username` | varchar nullable | |
| `phone` | varchar nullable | |
| `nik` | varchar nullable | national ID |
| `npwp` | varchar nullable | tax ID |
| `business_name` | varchar nullable | MSME profile shortcut |
| `business_address` | text nullable | |
| `created_at`, `updated_at` | timestamp | |

### `businesses`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `user_id` | FK → users | |
| `is_primary` | boolean | true for the main MSME |
| `name` | varchar | |
| `primary_kbli_code` | varchar | e.g. `56102` |
| `primary_kbli_description` | varchar | human-readable KBLI name |
| `legal_entity` | varchar | PT / CV / Perorangan |
| `scale` | varchar | mikro / kecil / menengah / besar |
| `revenue` | bigint nullable | annual revenue in IDR |
| `employee_count` | int nullable | |
| `address` | text nullable | |
| `nib` | varchar nullable | Nomor Induk Berusaha |
| `created_at`, `updated_at` | timestamp | |

> Auto-upserted on every `POST /api/permits/apply` inside the same DB transaction.

### `permit_applications`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `user_id` | FK → users | |
| `application_number` | varchar unique | auto-generated on submit |
| `kbli_code` | varchar | |
| `kbli_section` | varchar | KBLI section name |
| `permit_type` | varchar | NIB / Sertifikat Standar / Izin |
| `status` | varchar | draft / submitted / under_review / additional_docs_required / approved / rejected |
| `documents` | jsonb | `[{path, type, notes}]` — path validated as URL, type as enum |
| `applicant_notes` | text nullable | capped at 2000 chars |
| `reviewer_notes` | text nullable | set by admin on review |
| `submitted_at` | timestamp nullable | |
| `rejected_at` | timestamp nullable | |
| `created_at`, `updated_at` | timestamp | |

### `ai_interactions`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `session_id` | varchar | channel-user-uuid |
| `turn_index` | int | message sequence within session |
| `channel` | varchar | telegram / whatsapp / web / mobile / internal |
| `message_type` | varchar | user_message / ai_response / system_event |
| `intent` | varchar nullable | classified intent from `analyze_user_intent()` |
| `content` | text | |
| `user_id` | bigint nullable | FK → users |
| `response_time_ms` | int nullable | AI response latency |
| `is_flagged` | boolean | manual flag by admin |
| `created_at`, `updated_at` | timestamp | |

### `kbli_scrape_targets`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `kbli_code` | varchar | e.g. `56102` |
| `status` | varchar | pending / scraping / done / error |
| `scraped_content` | text | raw JSON from OSS scraper (added via ALTER TABLE) |
| `created_at`, `updated_at` | timestamp | |

### `magic_link_tokens`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `user_id` | FK → users | |
| `token` | varchar unique | random 32-char hex |
| `channel` | varchar | `email` / `telegram_link` |
| `expires_at` | timestamp | 15-min TTL for telegram_link, longer for email |
| `used_at` | timestamp nullable | set when consumed |
| `created_at`, `updated_at` | timestamp | |

### ChromaDB Document Schema (`oss_regulations`)
```json
{
  "id": "kbli_56102_chunk_3",
  "document": "Teks regulasi OSS untuk KBLI 56102...",
  "metadata": {
    "kbli_code": "56102",
    "section": "kewajiban",
    "skala": "Usaha Kecil",
    "source_url": "https://oss.go.id/...",
    "sub_chunk_index": 3
  },
  "embedding": [384-dim float vector]
}
```

---

## 8. Security Posture

| Layer | Measure |
|---|---|
| **CORS** | Locked to `FRONTEND_URL` env var — no wildcard `*` |
| **Chat widget auth** | Next.js `/api/ai/chat` validates Bearer token against `/api/auth/me`; `user_id` is server-extracted, client cannot impersonate |
| **Internal routes** | `X-Internal-Key` validated with `hash_equals` on all `/api/internal/*` |
| **Rate limiting (API)** | `throttle:120,1` on all `auth:sanctum` routes (Laravel) |
| **Rate limiting (AI)** | 5 messages/min per `user_id` in ai-engine (in-memory, friendly Indonesian rejection) |
| **Permit ownership** | `GET /api/permits/detail/{id}` checks `user_id === auth()->id()` — MSME cannot view others' permits |
| **Document validation** | Path validated as URL; type restricted to enum; notes capped at 2000 chars; max 20 docs per application |
| **Telegram linking** | Token 15-min TTL; duplicate chat_id returns 409; one-time use only |

---

## 9. Component Completion Status

### Pillar 1 — AI Engine (FastAPI + ChromaDB)

| Component | Status | Notes |
|---|---|---|
| Telegram webhook receiver | ✅ Done | Signature validation, `/start` routing |
| Telegram account linking handler | ✅ Done | `handle_telegram_link()` + `handle_start_command()` |
| WhatsApp webhook receiver | ⏳ Skeleton | Meta token not configured — Phase D |
| Web chat endpoint (`/webhook/chat`) | ✅ Done | Synchronous, called by Next.js API route |
| `analyze_user_intent()` | ✅ Done | JSON-mode Gemma 4b pre-call, KBLI extraction |
| `query_regulations()` RAG | ✅ Done | KBLI-prefixed query (n=8) or fallback (n=4) |
| `generate_ai_response()` | ✅ Done | systemInstruction, phase CTA enforcement |
| Conversation history | ✅ Done | Last 2 turns per user, passed as `contents[]` |
| Gemma retry + fallback | ✅ Done | 3 attempts, RAG-only fallback |
| AI-engine rate limiter | ✅ Done | 5 msg/min per user_id, friendly Indonesian error |
| `log_to_backend()` | ✅ Done | Fire-and-forget to Laravel |
| `/health` endpoint | ✅ Done | Returns model, chunk count, chroma_status |
| Data pipeline (scraper) | ✅ Running | 35+ KBLI done, 25 more queued |

### Pillar 2 — TALL Backend (Laravel + Filament)

| Component | Status | Notes |
|---|---|---|
| Auth (magic link) | ✅ Done | Passwordless email login |
| User MSME profile | ✅ Done | name, phone, NIK, NPWP, business_name/address via `PATCH /api/profile` |
| Telegram account linking | ✅ Done | Token generation, `/api/internal/telegram/link`, duplicate check |
| Telegram notifications | ✅ Done | `PermitApplicationObserver` — approved/rejected/under_review/additional_docs |
| Permit applications CRUD | ✅ Done | API + Filament resource; document/ownership validation |
| Permit detail endpoint | ✅ Done | `GET /api/permits/detail/{id}` with MSME ownership check |
| Business auto-population | ✅ Done | `Business::updateOrCreate()` in same transaction as permit apply |
| AI logs API | ✅ Done | `/api/internal/ai-logs` |
| User context API | ✅ Done | `/api/internal/user-context` (returns businesses[], telegram_chat_id) |
| Filament admin panel | ✅ Done | Styled with Ethereal Slate design |
| Filament UserStatsWidget | ✅ Done | UMKM users, Telegram-linked, active 24h, pending permits |
| Filament AI Interactions | ✅ Done | Session thread filter, view-thread action, 30s live poll |
| Pipeline trigger API | ✅ Done | `POST /api/pipeline/trigger` |
| API rate limiting | ✅ Done | `throttle:120,1` on auth:sanctum group |
| CORS lockdown | ✅ Done | Restricted to `FRONTEND_URL` |

### Pillar 3 — Next.js Frontend

| Component | Status | Notes |
|---|---|---|
| Magic link login page | ✅ Done | |
| Dashboard with real data | ✅ Done | SWR, permit count, status, KBLI name |
| LKPM reminder banner | ✅ Done | Fires for kecil/menengah permits >90 days old |
| Chat widget | ✅ Done | Collapsible, server-token-validated, typing indicator |
| Permit wizard (`/permits/new`) | ✅ Done | KBLI typeahead (debounced), multi-step apply |
| Permit list (`/permits`) | ✅ Done | Skeletons, empty state, status badges |
| Permit detail (`/permits/[id]`) | ✅ Done | SWR + dedicated API, NextActionCard (6 states), requirements checklist, docs list |
| Profile page — view | ✅ Done | All MSME fields displayed |
| Profile page — inline edit | ✅ Done | Save/cancel bar, `PATCH /api/profile` |
| Telegram connect flow | ✅ Done | Token generation, deep link button, "connected" badge |
| Mobile responsive | ✅ Done | BottomNav, `pb-24`, `sm:grid-cols-2` throughout |
| Skeleton loaders | ✅ Done | Dashboard, permits list, permit detail |
| Empty states | ✅ Done | `EmptyState` component on zero-permit views |
| Design system applied | ✅ Done | Ethereal Slate, Manrope, glassmorphism |
| Business dashboard (post-license) | ⏳ Not built | Phase 3 features |
| React Native mobile app | ⏳ Not started | Hackathon stretch goal |

---

## 10. Known Issues

| # | Issue | Severity | Status |
|---|---|---|---|
| 1 | WhatsApp webhook not connected (no Meta token) | Medium | Blocked — Phase D |
| 2 | Gemma response time 25–55s (acceptable for hackathon demo) | Info | Accepted |
| 3 | `sentence-transformers` version pinned (`huggingface-hub==0.27.0`) — fragile workaround | Low | Monitoring |
| 4 | ChromaDB has 35+ KBLI codes — OSS has thousands | Low | Known scope limit |
| 5 | AI-engine rate limiter is in-memory — resets on container restart | Low | Accepted for hackathon |
| 6 | Conversation history is in-memory — lost on container restart | Low | Accepted for hackathon |

> Previously tracked issues now resolved: business profile empty (fixed: auto-upserted on permit apply), no rate limiting (fixed: `throttle:120,1` on API + 5 msg/min in ai-engine).

---

---

# Status Snapshot — 2026-04-13

> **Updated:** 2026-04-13 | **Diff from:** 2026-04-10

## What Changed Since Last Snapshot

### Design System — Volcanic Amber (New)
The entire admin panel theme was migrated from **Ethereal Slate** (indigo) to **Volcanic Amber / Molten Command Center**:
- Base surface: `#0c0a09` (obsidian-black), `#1c1917`, `#292524`
- Primary gradient: amber `#d97706` → gold `#f59e0b`
- Frosted-glass cards with warm `backdrop-blur`
- Typeface: Manrope (unchanged)
- Layout: Top Navigation (replaces sidebar)
- Applied to: all Filament pages, widgets, and admin resources

> Previous "Ethereal Slate" (indigo) references in the status above are now outdated — the live panel uses Volcanic Amber.

### KBLI / PB UMKU Data Hub (New — Filament)
New Filament page at `/admin/data-import-hub`:
- Upload KBLI Excel + PB UMKU Excel via Filament file upload (supports up to 300 MB)
- Button: **"Sync ke ChromaDB"** → calls `POST data-pipeline:9000/pipeline/etl-excel`
- Replaces the old manual `scp` + script workflow

### Excel ETL Pipeline (New — `data-pipeline/etl_pipeline.py`)
Fully deterministic Pandas pipeline, **no LLM calls**:
- Reads `kbli.xlsx` + `pb_umku.xlsx` from `/app/data/raw_excel/`
- Handles merged-cell forward-fill
- Zero-pads KBLI codes to 5 digits (Excel stores as float)
- Expands multi-value `Skala Usaha` rows
- Merges PB UMKU relational data
- Generates semantic Markdown chunks
- Upserts into ChromaDB `oss_regulations` collection with full metadata
- Fix applied (2026-04-13): handles mid-word hyphenation in Excel cells (e.g. `pe-nerbitan` → `penerbitan`)

### Backend — phpoffice/phpspreadsheet Added
`phpoffice/phpspreadsheet` added to `backend-tall` composer dependencies to support Excel file processing within the Laravel/Filament layer (composer.lock updated).

### Data Pipeline — ETL API Endpoints
| Endpoint | Method | Description |
|---|---|---|
| `/pipeline/etl-excel` | POST | Trigger Excel → PostgreSQL → ChromaDB ETL |
| `/pipeline/etl-excel/status` | GET | Poll ETL job progress |

---

## Component Completion Status (2026-04-13)

### Pillar 1 — AI Engine (FastAPI + ChromaDB)

| Component | Status | Change |
|---|---|---|
| Telegram webhook receiver | ✅ Done | — |
| Web chat endpoint | ✅ Done | — |
| RAG + intent classification | ✅ Done | — |
| Data pipeline (scraper) | ✅ Running | 35+ KBLI done (Excel ETL now primary source) |
| **Excel ETL pipeline** | ✅ Done | **NEW** — deterministic, no LLM |
| **ETL API endpoints** | ✅ Done | **NEW** — `/pipeline/etl-excel` + status |
| WhatsApp | ⏳ Not configured | — |

### Pillar 2 — TALL Backend (Laravel + Filament)

| Component | Status | Change |
|---|---|---|
| All previously listed | ✅ Done | — |
| **Volcanic Amber theme** | ✅ Done | **NEW** — replaces Ethereal Slate |
| **Data Import Hub page** | ✅ Done | **NEW** — `/admin/data-import-hub` |
| **phpspreadsheet dep** | ✅ Done | **NEW** — Excel upload support |

### Pillar 3 — Next.js Frontend

| Component | Status | Change |
|---|---|---|
| All previously listed | ✅ Done | — |
| **Design references** | ⚠️ Check | Frontend still references Ethereal Slate tokens — may need Volcanic Amber audit |
| Business dashboard (post-license) | ⏳ Not built | — |
| React Native mobile app | ⏳ Not started | — |

---

## Known Issues (2026-04-13)

| # | Issue | Severity | Status |
|---|---|---|---|
| 1 | WhatsApp webhook not connected (no Meta token) | Medium | Blocked — Phase D |
| 2 | Gemma response time 25–55s | Info | Accepted |
| 3 | `sentence-transformers` version pinned (`huggingface-hub==0.27.0`) | Low | Monitoring |
| 4 | ChromaDB KBLI coverage: 35+ of thousands | Low | Excel ETL now faster path |
| 5 | AI-engine rate limiter / conversation history in-memory | Low | Accepted for hackathon |
| 6 | Next.js frontend may still use old Ethereal Slate color tokens | Low | Needs audit |

> **Resolved since 2026-04-10:** mid-word hyphenation in Excel ETL (fixed commit `21f07b0`); Excel ETL pipeline now production-ready and replaces PDF-based AI scraper as primary KBLI data ingestion path.
