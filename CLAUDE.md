# BIMA-AI Global Project Rules

## 🎯 Project Context
We are building BIMA-AI, a Hackathon project for DPMPTSP to unravel OSS RBA bureaucracy.
*   **Pillar 1:** Python/FastAPI + ChromaDB (AI Engine & RAG Pipeline)
*   **Pillar 2 & 3:** Next.js + React Native (Licensing Wizard & Super App UI)
*   **Core Backend:** Laravel 13 + Filament v.4 + PostgreSQL (TALL Stack)

### 🧠 Primary LLM: Hosted Gemma via Google AI Studio
*   **Model:** `gemma-3-27b-it` accessed via the Google Generative Language REST API
    (`generativelanguage.googleapis.com/v1beta/models/gemma-3-27b-it:generateContent`)
*   **Why Gemma:** Open-weights model hosted by Google — no VPS GPU needed, same API key/infra as Gemini, no vendor lock-in on weights.
*   **Key differences from Gemini:**
    - No `thinkingConfig` support — remove that field from all payloads
    - No API-level `response_mime_type: application/json` — enforce JSON via prompt only
    - Response parsing: strip Markdown code fences (` ```json ... ``` `) before `json.loads()` — Gemma sometimes wraps JSON in fences even when instructed not to
    - `finishReason` values: `STOP` / `MAX_TOKENS` (same as Gemini, no `thought` parts)
*   **Intent classification:** `analyze_user_intent()` in `ai_handler.py` does a lightweight JSON-mode pre-call to extract phase (1/2/3) and KBLI code before the main generation call. This tightens RAG queries for KBLI-specific questions.
*   **Env var:** `GEMINI_MODEL=models/gemma-3-27b-it` in `ai-engine/.env`

## 🎨 UI & Design System

For ANY frontend changes in Filament or Next.js, you MUST first read design.md and strictly apply its color codes, typography (Manrope), and glassmorphism rules. Do not use default Tailwind borders or pure black/white backgrounds.

The canonical design system is **Volcanic Amber / Molten Command Center** — deep obsidian-black base (`surface` #0c0a09, `surface_low` #1c1917, `surface_container` #292524), Manrope typeface, frosted glass cards with warm-tinted `backdrop-blur`, amber-to-gold primary gradient (#d97706 → #f59e0b), and **Top Navigation** layout. Previous theme was "Ethereal Slate" (indigo). The strict "No-Line Rule" still applies.

Reference file: **[design.md](./design.md)**

## 🤖 BIMA-AI Persona
The AI persona, 3-phase lifecycle (Pre-License → Execution → Post-License), tone rules, and
portal link directives are fully documented in **[BIMA_PERSONA.md](./BIMA_PERSONA.md)**.
All AI prompt tuning, system instruction changes, or new conversation flows MUST reference
that file first before modifying `ai-engine/services/ai_handler.py`.

## 🛠️ Core Operating Principles
1.  **Verify Before Moving On:** Never write massive blocks of code without testing. After creating or modifying any component (frontend or backend), you MUST run the appropriate build, lint, or test command to verify it works before proceeding to the next step.
2.  **Batch Full-Stack Scaffolding:** When asked to build a feature, consider the entire stack. For example, if building a CRUD feature, handle the migration, model, API controller, and admin resource simultaneously to ensure data consistency.
3.  **Leverage Sub-Agents:** If you encounter a framework-specific error (like a Next.js App Router issue or a deep Laravel exception), spawn a sub-agent to explore the documentation or internal files before blindly writing a fix.

## 🐘 Laravel & Filament Strict Conventions
*   **Authentication & Seeders:** When creating Laravel authentication or user seeders, **never hash passwords that are already being hashed** by model casts or mutators. Always check the `User` model for `$casts` with 'hashed' or `setPasswordAttribute` mutators before writing password logic. (This prevents the double-hashing bug).
*   **Filament Resources:** After generating or modifying Filament resources, you must run `php artisan filament:check` or attempt to load the admin panel to verify no type errors or property mismatches (like `$navigationGroup`) exist. Use string types, not enums, for `$navigationGroup` unless explicitly configured otherwise.
*   **Filament v4 Documentation:** When building or modifying the admin panel, you MUST strictly reference the Filament v4.x documentation (https://filamentphp.com/docs/4.x/). Do not use v3 syntax (e.g., `Filament\Forms\Components\Section` is now `Filament\Schemas\Components\Section`; `Form`/`Infolist` containers are now `Schema`).
*   **Database Migrations:** Always verify database connectivity with `php artisan migrate:status` before proceeding with new migrations or seeders.

## 🖥️ VPS & Infrastructure

### SSH Access
*   **SSH Command:** `ssh bima-vps` → `wdnsds@116.254.113.81:2222` using `~/.ssh/id_bima_vps`
*   **Project directory:** `~/bima-ai` on VPS
*   **Deploy (preferred — via git):**
    ```bash
    cd ~/bima-ai-project && git add -A && git commit -m "..." && git push
    ssh bima-vps "cd ~/bima-ai && git pull && docker compose up -d --build --remove-orphans"
    ```
    Then run migrations: `ssh bima-vps "cd ~/bima-ai && docker compose exec backend php artisan migrate --force && docker compose exec backend php artisan optimize:clear"`
*   **Vite theme build:** Node.js is not in the runtime container. Build assets locally or on VPS via fnm (`export PATH="$HOME/.local/share/fnm:$PATH" && eval "$(fnm env)"`), then commit `public/build/` to git.
*   **Deploy (legacy direct):** `ssh bima-vps "cd ~/bima-ai && docker compose pull && docker compose up -d --remove-orphans"`

### Service Map
| Service | Internal | Public | Notes |
|---|---|---|---|
| **nginx** | — | `:80` | Sole public entry point |
| **backend** (FrankenPHP) | `backend:80` | `:8000` (direct debug) | Laravel 13 + Filament |
| **ai-engine** (FastAPI) | `ai-engine:8000` | via `/webhook/` only | ChromaDB embedded |
| **postgres** | `postgres:5432` | — | internal only |
| **redis** | `redis:6379` | — | internal only |
| **frontend** | — | Vercel | decoupled, git auto-deploy |
| **data-pipeline** (FastAPI) | `data-pipeline:9000` | — | ETL + Playwright scraper |

### KBLI / PB UMKU Data Architecture
*   **Source of truth:** PostgreSQL tables `kblis` (14 columns incl. `sektor`) and `pb_umkus` (7 columns)
*   **Excel headers (kbli.xlsx):** No | Kode KBLI | Judul KBLI | Ruang Lingkup | Skala Usaha | Tingkat Risiko | Perizinan Berusaha | Persyaratan | Jangka Waktu Penerbitan | Kewajiban | PB UMKU | Parameter | Kewenangan | **Sektor**
*   **Excel headers (pb umku.xlsx):** No | Nomeklatur PB UMKU | Persyaratan | Jangka Waktu Penerbitan | Kewajiban | Masa Berlaku | Parameter | Kewenangan
*   **KBLI codes are 5 digits, zero-padded** (e.g. `03111` not `3111`). Excel stores them as floats — always `zfill(5)`.
*   **Import flow:** Upload Excel via Filament Data Import Hub (`/admin/data-import-hub`) → truncate+parse to PostgreSQL → click "Sync ke ChromaDB" → `POST data-pipeline:9000/pipeline/etl-excel` → runs `etl_pipeline.py` → rebuilds `oss_regulations` collection in ChromaDB
*   **ETL script:** `data-pipeline/etl_pipeline.py` — deterministic Pandas pipeline, no LLM calls. Handles merged-cell forward-fill, multi-value Skala Usaha expansion, PB UMKU relational merge, semantic chunking, ChromaDB upsert with metadata filters.
*   **Raw Excel files on VPS:** `data-pipeline/data/raw_excel/kbli.xlsx` and `pb_umku.xlsx` (mounted at `/app/data/raw_excel/` inside container)

### Nginx Routing (port 80)
*   `/webhook/` → `ai-engine:8000`
*   `/api`, `/sanctum`, `/admin`, `/livewire`, `/css`, `/js`, `/fonts`, `/storage` → `backend:80`
*   `/` → `302 /admin`

### URLs
*   **Filament Admin:** `http://116.254.113.81/admin` ✅ styled & confirmed
*   **Backend direct:** `http://116.254.113.81:8000`
*   **Frontend:** `https://project-5z22k.vercel.app` — auto-deploy on push to `main`, repo `ptstpjateng/bima-ai`, root dir `frontend/`
*   **CORS / API links:** always use `https://project-5z22k.vercel.app` as the frontend origin, never the old IP or localhost

---

## ⚙️ Environment Variables Reference

### Root `.env` (Docker Compose shared vars — `/.env`)
```
DB_DATABASE=bima_ai
DB_USERNAME=bima
DB_PASSWORD=<see VPS>
```

### Backend — `backend-tall/.env`
| Key | Local | Production (VPS) |
|---|---|---|
| `APP_ENV` | `local` | `production` |
| `APP_URL` | `http://localhost` | `http://116.254.113.81` |
| `APP_DEBUG` | `true` | `false` |
| `DB_CONNECTION` | `pgsql` | `pgsql` |
| `DB_HOST` | `127.0.0.1` | `postgres` (Docker DNS) |
| `DB_DATABASE` | `bima` | `bima_ai` |
| `DB_USERNAME` | `postgres` | `bima` |
| `SESSION_DRIVER` | `database` | `redis` |
| `CACHE_STORE` | `database` | `redis` |
| `QUEUE_CONNECTION` | `database` | `redis` |
| `REDIS_HOST` | `127.0.0.1` | `redis` (Docker DNS) |
| `FRONTEND_URL` | — | `https://project-5z22k.vercel.app` |
| `INTERNAL_API_KEY` | — | `<see VPS>` |
| `AI_ENGINE_URL` | — | `http://ai-engine:8000` |

> **APP_KEY** and **DB_PASSWORD** are secrets — check VPS `.env` directly: `ssh bima-vps "grep -E 'APP_KEY|DB_PASSWORD' ~/bima-ai/backend-tall/.env"`

### AI Engine — `ai-engine/.env` (VPS only, not committed)
| Key | Notes |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio key — `models/gemini-2.5-flash` |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_SECRET_TOKEN` | Webhook validation secret |
| `WHATSAPP_API_TOKEN` | Meta permanent token (not yet configured) |
| `LARAVEL_BACKEND_URL` | `http://backend:80` |
| `LARAVEL_API_KEY` | Must match backend `INTERNAL_API_KEY` |
| `CHROMA_HOST` | `ai-engine` |

### Data Pipeline — `data-pipeline/.env` (VPS only)
| Key | Value | Notes |
|---|---|---|
| `GEMINI_API_KEY` | `<see VPS>` | Primary LLM — Google AI Studio key |
| `GEMINI_MODEL` | `models/gemini-2.5-flash` | Primary model (~5–10s per KBLI) |
| `OLLAMA_HOST` | `http://172.19.0.1:11435` | Fallback LLM — Docker gateway → host proxy on port 11435 |
| `OLLAMA_MODEL` | `gemma4` | Fallback model (CPU-only, ~15 min per KBLI — only used if Gemini fails) |
| `DB_HOST` | `postgres` | Docker DNS |
| `DB_DATABASE` | `bima_ai` | |
| `DB_USERNAME` | `bima` | |
| `DB_PASSWORD` | `<see VPS>` | |
| `CHROMA_DB_PATH` | `/app/chroma_db` | Matches `chroma_data` Docker volume |

> **Ollama proxy:** The pipeline container cannot reach `127.0.0.1:11434` directly. A Python TCP proxy (`~/ollama-proxy.py`) runs in a `screen` session named `ollama-proxy`, forwarding `0.0.0.0:11435 → 127.0.0.1:11434`. If the proxy dies, restart with: `screen -dmS ollama-proxy python3 ~/ollama-proxy.py`

> **Pipeline worker (scraper):** `data-pipeline/run_pipeline_ollama.py` — called by `server.py` via `POST /pipeline/trigger?limit=N`. Reads `kbli_scrape_targets` for `status='pending'`, marks as `scraping`, scrapes OSS with Playwright (tabs + accordions), extracts with Gemini (falls back to Ollama), converts JSON to semantic Markdown chunks in ChromaDB, saves raw JSON to `scraped_content` column, sets `status='done'`.

> **Pipeline worker (Excel ETL):** `data-pipeline/etl_pipeline.py` — called by `server.py` via `POST /pipeline/etl-excel`. Pure deterministic Pandas pipeline (no LLM). Reads Excel files from `/app/data/raw_excel/`, cleans merged cells, zero-pads KBLI codes, expands multi-value Skala Usaha, merges PB UMKU relations, generates semantic Markdown chunks, upserts to ChromaDB `oss_regulations` collection. Status: `GET /pipeline/etl-excel/status`.

> **Note on table name:** The scrape queue is `kbli_scrape_targets` (not `knowledge_bases`). The `scraped_content TEXT` column was added manually via `ALTER TABLE`.

### Frontend — `frontend/.env.local` (local) / Vercel dashboard (production)
| Key | Local | Production |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://backend-tall.test` | `http://116.254.113.81` |

> Set production env vars in **Vercel Dashboard → Project → Settings → Environment Variables**. Never commit `.env.local`.

## ⚛️ Next.js & React Native Conventions
*   **Strict Typing:** All components must be strictly typed with TypeScript. Do not use `any`.
*   **UI/UX:** Prioritize a nice, clean, and clear UI/UX. Use whitespace effectively and ensure skeleton loaders or loading states are implemented for any data fetching.
*   **Validation:** Run `npm run lint` and `npm run build` frequently during frontend development to catch hydration or type mismatch errors early.
