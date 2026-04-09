# BIMA-AI — Project Status, System Architecture & Schema

> **Last updated:** 2026-04-10  
> **Hackathon:** DPMPTSP Jawa Tengah — OSS RBA AI Assistant

---

## 1. Executive Summary

BIMA-AI is a production-deployed, omnichannel AI assistant that guides Indonesian UMKM owners through the full business licensing lifecycle (OSS RBA). The core messaging pipeline (Telegram → FastAPI → Gemma → ChromaDB → Reply) is **live and working**. The Next.js frontend portal is **live on Vercel**. The Laravel admin panel is **live on VPS**.

**What works end-to-end today:**
- Telegram bot receives messages → classifies intent → queries ChromaDB → calls Gemma → replies in clean Indonesian
- Next.js portal: magic-link auth, permit wizard UI, profile page, dashboard skeleton
- Filament admin: user/permit/KBLI/AI-log management
- 35 KBLI codes fully scraped and indexed (274 semantic chunks in ChromaDB)

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
| **Telegram Bot** | ✅ Live | webhook configured, messages processing |
| **WhatsApp Bot** | ⏳ Not configured | Meta token placeholder — not yet set up |
| **Data Pipeline** | ✅ Done (35/35 KBLI) | `data-pipeline` service, run on-demand |

---

## 3. System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        USER CHANNELS                                 │
│   Telegram Bot              WhatsApp (Meta Cloud)                    │
│   @bima_ai_bot              (not yet configured)                     │
└──────────────┬──────────────────────────────────────────────────────┘
               │  HTTPS webhook POST
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  VPS  116.254.113.81  (Docker Compose)                               │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  nginx :80  (sole public entry)                             │    │
│  │   /webhook/*   → ai-engine:8000                             │    │
│  │   /api, /admin, /sanctum, /storage → backend:80             │    │
│  │   /           → 302 /admin                                  │    │
│  └──────────────────┬───────────────────┬───────────────────────┘   │
│                     │                   │                            │
│         ┌───────────▼──────┐  ┌─────────▼──────────────────────┐   │
│         │  FastAPI AI      │  │  Laravel 13 + FrankenPHP        │   │
│         │  ai-engine:8000  │  │  backend:80                     │   │
│         │                  │  │                                 │   │
│         │  /webhook/tg     │  │  /api/internal/ai-logs          │   │
│         │  /webhook/wa     │  │  /api/internal/user-context     │   │
│         │  /vectorize      │  │  /api/auth  (magic link)        │   │
│         │  /health         │  │  /api/permits                   │   │
│         │                  │  │  /admin  (Filament)             │   │
│         │  Services:       │  │  /api/pipeline/trigger          │   │
│         │  ├ ai_handler    │  │                                 │   │
│         │  ├ rag_service   │  │  Queue worker (redis)           │   │
│         │  ├ user_context  │  └──────────┬──────────────────────┘   │
│         │  └ telegram_poll │             │                           │
│         └───────┬──────────┘  ┌──────────▼──────┐  ┌────────────┐  │
│                 │             │  PostgreSQL 16   │  │  Redis 7   │  │
│         ┌───────▼──────────┐  │  :5432 internal │  │  :6379     │  │
│         │  ChromaDB        │  │                 │  │  sessions  │  │
│         │  (embedded,      │  │  Tables:        │  │  queues    │  │
│         │   persistent)    │  │  users          │  │  cache     │  │
│         │  274 chunks      │  │  businesses     │  └────────────┘  │
│         │  35 KBLI codes   │  │  permit_applic. │                  │
│         │  collection:     │  │  ai_interactions│                  │
│         │  oss_regulations │  │  kbli_scrape_t. │                  │
│         └──────────────────┘  │  knowledge_base │                  │
│                               │  magic_link_tok.│                  │
│                               └─────────────────┘                  │
└─────────────────────────────────────────────────────────────────────┘
               │
               │  External APIs
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Google Generative Language API                                      │
│  models/gemma-4-26b-a4b-it  (MoE: 26B total, 4B active)            │
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
        ├─ Deduplicate (update_id cache)
        │
        ▼
process_message()   [ai_handler.py]
        │
        ├─ 1. analyze_user_intent()  ──── Gemma JSON call ──► phase (1/2/3) + KBLI code
        ├─ 2. fetch_user_context()  ───── GET /api/internal/user-context (Laravel)
        │       └─ business profile, license vault, Telegram↔user binding
        ├─ 3. query_regulations()   ───── ChromaDB semantic search
        │       └─ KBLI-prefixed query if KBLI detected (n=8), else n=4
        │
        ├─ 4. Build systemInstruction (system prompt + user ctx + RAG chunks)
        │
        ├─ 5. _call_gemma_with_retry()  ─ Google API (3 attempts, backoff 1s/2s)
        │       └─ Filters thought=True parts (gemma-4 thinking model)
        │       └─ Falls back to _rag_fallback_response() if all retries fail
        │
        ├─ 6. _send_telegram_reply()  ── Telegram sendMessage (Markdown + inline button)
        │
        └─ 7. log_to_backend()  ──────── POST /api/internal/ai-logs (fire-and-forget)
```

---

## 5. AI Pipeline Detail

### 5.1 LLM

| Property | Value |
|---|---|
| **Model** | `models/gemma-4-26b-a4b-it` |
| **Architecture** | MoE — 26B total params, ~4B active |
| **Hosting** | Google AI Studio (same infra as Gemini) |
| **No GPU on VPS** | ✅ — all inference is remote |
| **thinkingConfig** | Not sent (model handles thinking internally) |
| **Thought filtering** | Parts with `thought=True` stripped from response |
| **Timeout** | 120s per call |
| **Max output tokens** | 2048 (main), 128 (intent) |
| **Retry** | 3 attempts, 1s / 2s backoff on 429/503 |
| **Fallback** | RAG-only response if all retries fail |

### 5.2 Embeddings (RAG)

| Property | Value |
|---|---|
| **Model** | `paraphrase-multilingual-MiniLM-L12-v2` |
| **Dimensions** | 384 |
| **Normalisation** | L2 (cosine distance in ChromaDB) |
| **Used in** | Both ingest (data-pipeline) and query (rag_service) |
| **Loaded as** | Module-level singleton (`_get_embedder()`) |

### 5.3 ChromaDB

| Property | Value |
|---|---|
| **Collection** | `oss_regulations` |
| **Distance metric** | cosine (`hnsw:space=cosine`) |
| **Total chunks** | 274 |
| **KBLI codes indexed** | 35 |
| **Persistence** | Docker volume `chroma_data` at `/app/chroma_db` |

---

## 6. Database Schema (PostgreSQL)

### `users`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `name` | varchar | |
| `email` | varchar unique | |
| `password` | varchar | hashed (cast) |
| `telegram_id` | varchar | links Telegram user to account |
| `telegram_username` | varchar | |
| `phone` | varchar | |
| `business_name` | varchar | MSME profile |
| `kbli_code` | varchar | primary KBLI |
| `business_scale` | varchar | Mikro/Kecil/Menengah/Besar |
| `nik` | varchar | national ID |
| `npwp` | varchar | tax ID |
| `created_at`, `updated_at` | timestamp | |

### `businesses`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `user_id` | FK → users | |
| `name` | varchar | |
| `kbli_code` | varchar | |
| `legal_entity` | varchar | PT / CV / Perorangan |
| `scale` | varchar | |
| `address` | text | |
| `nib` | varchar | Nomor Induk Berusaha |
| `created_at`, `updated_at` | timestamp | |

### `permit_applications`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `user_id` | FK → users | |
| `kbli_code` | varchar | |
| `permit_type` | varchar | NIB / Sertifikat Standar / Izin |
| `status` | varchar | draft / submitted / approved / rejected |
| `documents` | jsonb | uploaded document manifest |
| `submitted_at` | timestamp | |
| `created_at`, `updated_at` | timestamp | |

### `ai_interactions`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `session_id` | varchar | channel-user-uuid |
| `turn_index` | int | 0=user, 1=ai |
| `channel` | varchar | telegram / whatsapp |
| `message_type` | varchar | user_message / ai_response |
| `content` | text | |
| `user_id` | bigint nullable | FK → users |
| `created_at`, `updated_at` | timestamp | |

### `kbli_scrape_targets`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `kbli_code` | varchar | e.g. `56102` |
| `status` | varchar | pending / scraping / done / error |
| `scraped_content` | text | raw JSON from OSS scraper |
| `created_at`, `updated_at` | timestamp | |

### `knowledge_base_articles`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `title` | varchar | |
| `content` | text | |
| `kbli_code` | varchar | |
| `category` | varchar | |
| `created_at`, `updated_at` | timestamp | |

### `magic_link_tokens`
| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `user_id` | FK → users | |
| `token` | varchar unique | |
| `expires_at` | timestamp | |
| `used_at` | timestamp nullable | |

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

## 7. Component Completion Status

### Pillar 1 — AI Engine (FastAPI + ChromaDB)

| Component | Status | Notes |
|---|---|---|
| Telegram webhook receiver | ✅ Done | Signature validation, dedup |
| WhatsApp webhook receiver | ⏳ Skeleton | Meta token not configured |
| `analyze_user_intent()` | ✅ Done | JSON-mode Gemma pre-call |
| `query_regulations()` RAG | ✅ Done | Multilingual embeddings, metadata fixed |
| `generate_ai_response()` | ✅ Done | systemInstruction, thought filtering |
| Gemma retry + fallback | ✅ Done | 3 attempts, RAG-only fallback |
| `log_to_backend()` | ✅ Done | Fire-and-forget to Laravel |
| `/vectorize` endpoint | ✅ Done | Manual re-index trigger |
| Data pipeline (scraper) | ✅ Done | 35/35 KBLI scraped, 274 chunks indexed |

### Pillar 2 — TALL Backend (Laravel + Filament)

| Component | Status | Notes |
|---|---|---|
| Auth (magic link) | ✅ Done | Passwordless email login |
| User MSME profile | ✅ Done | kbli_code, scale, NIK, NPWP fields |
| Telegram↔account binding | ✅ Done | `telegram_id` on users table |
| Permit applications CRUD | ✅ Done | API + Filament resource |
| AI logs API | ✅ Done | `/api/internal/ai-logs` |
| User context API | ✅ Done | `/api/internal/user-context` |
| Filament admin panel | ✅ Done | Styled with Ethereal Slate design |
| Queue worker | ✅ Done | Redis-backed, separate container |
| Business profile model | ✅ Done | Separate from users table |
| Pipeline trigger API | ✅ Done | `POST /api/pipeline/trigger` |

### Pillar 3 — Next.js Frontend

| Component | Status | Notes |
|---|---|---|
| Magic link login page | ✅ Done | |
| Dashboard skeleton | ✅ Done | Loading states, skeleton UI |
| Permit wizard (`/permits`) | ✅ Done | Multi-step apply flow |
| Permit detail (`/permits/[id]`) | ✅ Done | |
| Profile page | ✅ Done | |
| Design system applied | ✅ Done | Ethereal Slate, Manrope, glassmorphism |
| Auth context | ✅ Done | |
| Business dashboard (post-license) | ⏳ Not built | Phase 3 features |
| React Native mobile app | ⏳ Not started | Hackathon stretch goal |

---

## 8. Known Issues

| # | Issue | Severity | Status |
|---|---|---|---|
| 1 | `analyze_user_intent` KBLI extraction not used in live queries (KBLI passed but RAG query targeting needs verification) | Medium | Open |
| 2 | WhatsApp webhook not connected (no Meta token) | Medium | Blocked (credentials) |
| 3 | Business profile not populated for any user (0 businesses in DB) | Medium | Open |
| 4 | ChromaDB only has 35 KBLI codes — OSS has thousands | Low | Known scope limit |
| 5 | `sentence-transformers` version conflict (`huggingface_hub` API breaking change) — worked around by downgrade but fragile | Low | Monitoring |
| 6 | No rate limiting on Telegram webhook endpoint | Low | Open |
| 7 | Gemma response time 25–55s (acceptable for hackathon, not production) | Info | Accepted |
