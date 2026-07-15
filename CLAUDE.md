# BIMA-AI Global Project Rules

> **Operations:** for live deploy commands, common errors, rollback, and the
> pre-demo checklist see [`BIMA-Vault/Operations Runbook.md`](../BIMA-Vault/Operations%20Runbook.md).
> This file documents conventions; the runbook documents commands.

## 🎯 Project Context
We are building BIMA-AI, a Hackathon project for DPMPTSP to unravel OSS RBA bureaucracy.
*   **Pillar 1:** Python/FastAPI + ChromaDB (AI Engine & RAG Pipeline)
*   **Pillar 2:** Next.js 16 admin (`admin/`) + Next.js 16 portal (`portal/`) — both on Vercel
*   **Pillar 3:** Admin-API (FastAPI + SQLAlchemy 2 async, `admin-api/`) — replacing the Laravel admin surface page-by-page
*   **Legacy Core Backend:** Laravel 13 + Filament v.4 + PostgreSQL (TALL Stack) — still running, being migrated out

### 📊 Current state (as of 2026-07-15, Sprint D in progress)

*   **Sprints completed:** A (stabilize), B.1 (admin-api scaffold + admin shell), B.2 (read-only resource pages), C.4 (ingestion upload UI + reconciler), C.5 (architecture-flows visualizer), **officer-form-fill arc (2026-07, bima-ai PR #131 + SIAP #32)**. **Sprint D (rehearsals + backup video) is IN PROGRESS.**
*   **Officer copilot (WhatsApp, live on Beta):** the officer copilot fills the SIAP applicant *Formulir Isian* (form 560) from docs + profile via the no-hallucination engine, **computes GT from vessel dimensions** (0.25·L·B·D·f, `BIMA_GT_BLOCK_COEFFICIENT`), reads `thn_bangun` from the Surat Pesanan, drafts the real SK as a **LibreOffice-rendered PDF** (PDF-only), writes the **No. PPKP** to the officer Penomoran form (768, `set_ppkp_number`), **gates the draft** until the form is filled, and **auto-resends** a document on transient Meta 131053. Gemini `thinkingConfig.thinkingBudget:0` for reliable tool-calling. The citizen submission **auto-fills form 560** at submit. Applicant form = 560 (data fields + up_* slots); officer form = 768 (`no_ppkp`); `tgl_penetapan` is SIAP-set at TTE, not a form field.
*   **Live data:** 1,405 KBLI codes / 6,211 kblis rows / 319 pb_umkus rows / **6,775 ChromaDB chunks** (6,340 KBLI/PB-UMKU + 379 SIAP-license B1/B3 + 56 active-regulation **B2** chunks from `data-pipeline/siap_corpus.py`) / 11 live UMKM users.
*   **Live URLs:** see Service Map below.
*   **🌐 Domain (2026-07-15):** cut over **`nolongin.com` → `bimaptsp.com`** (webhook + every citizen/SIAP link; bima-ai PR #134, deployed + runtime-verified). **Citizen tracking is now SIAP-hosted at `beta-siap.bimaptsp.com/track/{ticket}`** (no login) — replacing the separate Vercel portal, per the "BIMA is a layer inside SIAP" repositioning. nolongin stays trusted/allowlisted during the soak; retire as a 301-redirect, not a hard-delete. See [[domain-cutover-bimaptsp]].
*   **WhatsApp UX:** sub-second typing acknowledgment (text bubble; APTANA doesn't expose Meta's native indicator — see [[Decisions]] §9). Final reply ~9–13 s.
*   **⚠️ Infra (2026-07-14):** the VPS is a Xen VM behind a MikroTik router. A reboot once handed the VM a DHCP IP (`10.10.10.2`) that didn't match the MikroTik dst-nat forward for `116.254.113.81` (→`10.10.10.8`), taking BIMA fully offline (no webhook in, no reply out). `eth0` is now **pinned static to `10.10.10.8`** in `/etc/netplan/00-installer-config.yaml`. If BIMA ever goes silent after a reboot, check `ip -brief a` == `.8` first.

### 🧠 Primary LLM: Gemini 2.5 Flash via Google AI Studio
*   **Model:** `gemini-2.5-flash` accessed via the Google Generative Language REST API
    (`generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent`)
*   **Env var:** `GEMINI_MODEL=models/gemini-2.5-flash` in `ai-engine/.env`
*   **Fallback ladder** (`ai_handler.py`; override via `GEMINI_FALLBACK_MODELS` env, comma-separated): on 429/503/network errors the citizen-chat path walks **`gemini-2.5-flash` → `gemini-2.5-pro` → `gemini-2.0-flash`** — version-diverse so a single model-line quota/outage doesn't take down the whole ladder. All rungs share the one API key. `license_resolver.py` and `submission_intent.py` run their own lightweight JSON-only pre-calls on the same precedence (`GEMINI_INTENT_MODEL` → `GEMINI_MODEL`).
*   **⚠️ Billing — prepay is mandatory:** the Google AI Studio project MUST have a prepayment method set up (Billing → *Set up prepay*). Without it, even a paid **Tier 1** account is flagged *"Prepay required"* and rate-restricted, so **every call 429s regardless of spend** — this took BIMA fully offline on 2026-06-05 (the whole fallback ladder 429'd; BIMA could only send static apologies). Keep a **Monthly spend cap** set as the blast-radius backstop, and note the secret-redaction below.
*   **Secret hygiene:** httpx logs the request URL at INFO and the API key rides in `?key=`. `logging_setup.py` (`configure_logging()` at `main.py` startup) redacts `generativelanguage.googleapis.com/...?key=` so the key never lands in container logs. If the key is ever exposed, rotate it in AI Studio → API Keys.
*   **Intent classification:** `analyze_user_intent()` in `ai_handler.py` does a lightweight JSON-mode pre-call to extract phase (1/2/3) and KBLI code before the main generation call. This tightens RAG queries for KBLI-specific questions.
*   **Response parsing:** strip Markdown code fences (` ```json ... ``` `) before `json.loads()` — the models sometimes wrap JSON in fences even when told not to.

> **On Gemma / the open-weights story:** BIMA originally ran **`gemma-3-27b-it`** as primary — open-weights, hosted by Google (no VPS GPU, same API key/infra as Gemini, "no vendor lock-in on weights"). **Google has since RETIRED Gemma 3** — `gemma-3-27b-it` now **404s** on `generateContent`. The live open-weights line is **`gemma-4-31b-it`** (or `gemma-4-26b-a4b-it`). To put Gemma back in the loop, point `GEMINI_MODEL` / `GEMINI_FALLBACK_MODELS` at a Gemma-4 model — but mind the **Gemma payload differences** vs Gemini: no `thinkingConfig` field, no API-level `response_mime_type: application/json` (enforce JSON via prompt only), and strip code fences before parsing. `finishReason` values are the same (`STOP` / `MAX_TOKENS`, no `thought` parts). The default ladder is kept all-Gemini for drop-in payload compatibility.

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
| **proxy** (Caddy 2) | — | `:80`, `:443` | Sole public entry point. Auto Let's Encrypt for `bimaptsp.com` + `beta-siap.bimaptsp.com` (still serves `nolongin.com` during the soak). Replaces `nginx` (legacy config archived in `nginx/`). |
| **backend** (FrankenPHP) | `backend:80` | `:8000` (direct debug) | Laravel 13 + Filament. Legacy admin at `bimaptsp.com/admin`. Being migrated out page-by-page. |
| **ai-engine** (FastAPI) | `ai-engine:8000` | via Caddy `/webhook/*` | ChromaDB embedded. Reads `chroma_data` named volume (shared with data-pipeline). APTANA WhatsApp inbound + outbound sender. |
| **admin-api** (FastAPI) | `admin-api:8001` | via Caddy `/admin-api/*` | NEW — replaces Laravel admin. Auth + dashboard + KBLI + AI interactions + ingestion. Status reconciler loop polls data-pipeline every 5 s. |
| **data-pipeline** (FastAPI) | `data-pipeline:9000` | — internal only — | ETL (`etl_pipeline.py` deterministic Pandas) + Playwright OSS scraper. Writes to ChromaDB AND mirrors to PostgreSQL `kblis`/`pb_umkus`. |
| **postgres** (16-alpine) | `postgres:5432` | — | Internal only. Single DB `bima_ai`. |
| **redis** (7-alpine) | `redis:6379` | — | Internal only. Sessions/cache/queue for Laravel. |
| **queue** (Laravel worker) | — | — | Same image as backend; runs `artisan queue:work`. |
| **bima-admin** (Next 16) | — | `admin.bimaptsp.com` (Vercel — consolidating into Beta-SIAP) | Next.js 16 + shadcn/ui + Midnight Government brand. Pages: `/dashboard`, `/ai-interactions`, `/kbli`, `/data` (ingestion), `/architecture` (system-flows visualizer). |
| **bima-portal** (Next 16) | — | `portal.bimaptsp.com` (Vercel — retiring; tracking moved to Beta-SIAP `/track`) | Public landing in `portal/` dir. Next 16 + Tailwind 4 + Framer Motion. Replaces broken legacy `frontend/`. |
| **legacy `frontend/`** | — | — | Deprecated. Builds broken; do not deploy. Will be deleted in Sprint D cleanup. |

> **Vercel Hobby gotcha:** `bima-admin` and `bima-portal` are on team `pusdatindpmptspjateng-3132` (Hobby plan). Commits authored by anyone NOT on the team are auto-blocked. See [[Decisions]] §10 — local git author identity for the BIMA repo MUST be `pusdatin.dpmptspjateng@gmail.com`, and PRs that touch `admin/` or `portal/` MUST be **rebase-merged**, not squashed.

### KBLI / PB UMKU Data Architecture
*   **Source of truth:** PostgreSQL tables `kblis` (14 columns incl. `sektor`) and `pb_umkus` (7 columns)
*   **Excel headers (kbli.xlsx):** No | Kode KBLI | Judul KBLI | Ruang Lingkup | Skala Usaha | Tingkat Risiko | Perizinan Berusaha | Persyaratan | Jangka Waktu Penerbitan | Kewajiban | PB UMKU | Parameter | Kewenangan | **Sektor**
*   **Excel headers (pb umku.xlsx):** No | Nomeklatur PB UMKU | Persyaratan | Jangka Waktu Penerbitan | Kewajiban | Masa Berlaku | Parameter | Kewenangan
*   **KBLI codes are 5 digits, zero-padded** (e.g. `03111` not `3111`). Excel stores them as floats — always `zfill(5)`.
*   **Import flow:** Upload Excel via Filament Data Import Hub (`/admin/data-import-hub`) → truncate+parse to PostgreSQL → click "Sync ke ChromaDB" → `POST data-pipeline:9000/pipeline/etl-excel` → runs `etl_pipeline.py` → rebuilds `oss_regulations` collection in ChromaDB
*   **ETL script:** `data-pipeline/etl_pipeline.py` — deterministic Pandas pipeline, no LLM calls. Handles merged-cell forward-fill, multi-value Skala Usaha expansion, PB UMKU relational merge, semantic chunking, ChromaDB upsert with metadata filters.
*   **Raw Excel files on VPS:** `data-pipeline/data/raw_excel/kbli.xlsx` and `pb_umku.xlsx` (mounted at `/app/data/raw_excel/` inside container)

### Caddy Routing (`caddy/Caddyfile`, ports 80 + 443)
*   `/webhook/*` → `ai-engine:8000` (read_timeout 120s for Gemini latency)
*   `/dl/*` → `ai-engine:8000` (rate-limit 120/min) — PPKP doc-prep PDFs. BIMA generates the sign-required docs (Pakta Integritas etc.), hosts them in-memory at `/dl/{token}` (AES-128-encrypted with the citizen's NIK, unguessable token, 15-min TTL + burns after ~5 fetches, no-store/noindex headers — `ai-engine/services/generated_docs.py` + `routers/downloads.py`), and APTANA fetches them to deliver as WhatsApp **document** messages. ⚠️ **Caddy reload didn't apply this route via `caddy reload` — needed `docker compose restart proxy`** (admin API returns 0 bytes, so `caddy reload` silently no-ops here; restart re-reads the mounted Caddyfile).
*   `/admin-api/*` → `admin-api:8001` (handle_path strips the prefix)
*   `/` (bare root) → `301 https://portal.bimaptsp.com` (anonymous visitors land on the public portal, not the legacy admin login)
*   everything else (`/api`, `/sanctum`, `/admin`, `/livewire`, `/css`, `/js`, `/fonts`, `/storage`, `/up`, …) → `backend:80` (read_timeout 300s for Filament Excel imports)

### URLs (current)
> **Domain cutover 2026-07-15:** live domain is now **`bimaptsp.com`** (was `nolongin.com`). nolongin.com is still served/trusted/allowlisted during the soak — retire it later as a **301-redirect**, not a hard-delete, to preserve old `/track` links already in the wild. See [[domain-cutover-bimaptsp]].
*   **Beta-SIAP (the system BIMA layers *inside*):** `https://beta-siap.bimaptsp.com` (Laravel 13 + Filament, on the VPS)
*   **Citizen tracking (canonical, no login):** `https://beta-siap.bimaptsp.com/track/{ticket}` — SIAP-hosted (`TrackingController::byTicket` + `track.blade.php`); matches the Meta citizen templates. Replaces the old `portal.*/track`.
*   **WhatsApp webhook:** `https://bimaptsp.com/webhook/aptana/inbound/{secret}` (APTANA Worker target — cut over + verified 2026-07-15)
*   **Admin API:** `https://bimaptsp.com/admin-api/*` (FastAPI on VPS)
*   **Legacy Filament admin:** `https://bimaptsp.com/admin` (still up; being migrated)
*   **Public portal (Vercel `bima-portal`):** `https://portal.bimaptsp.com` — **being retired**; tracking moved to Beta-SIAP `/track`.
*   **Admin console (Vercel `bima-admin`):** `https://admin.bimaptsp.com` — login `admin@bima.ai` / `BimaAdmin2026!`; being consolidated into Beta-SIAP.
*   **Bare domain:** `https://bimaptsp.com` → 301 → portal
*   **Live WhatsApp:** `+62 851 1755 7091` (APTANA-provisioned)
*   **CORS / TrustedHost:** `admin-api` + `ai-engine` allow `bimaptsp.com` + `*.bimaptsp.com` **and** the nolongin hosts during the soak (Caddy proxies preserve the inbound `Host`).

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

### Admin (`admin/.env.local` local / Vercel `bima-admin` production)
| Key | Local | Production |
|---|---|---|
| `NEXT_PUBLIC_ADMIN_API_URL` | `http://localhost:8001` | `https://bimaptsp.com/admin-api` |
| `NEXTAUTH_URL` | `http://localhost:3000` | `https://admin.bimaptsp.com` |
| `NEXTAUTH_SECRET` | dev value | strong random; in Vercel env |

### Portal (`portal/.env.local` local / Vercel `bima-portal` production)
| Key | Local | Production |
|---|---|---|
| (mostly static; no API calls today) | — | — |

### Legacy frontend (`frontend/`) — DEPRECATED
The old Next.js portal at `frontend/` is not built or deployed anywhere. Replaced by `portal/`. Will be removed in Sprint D cleanup. Do not edit.

> Set production env vars in **Vercel Dashboard → Project → Settings → Environment Variables**. Never commit `.env.local`.

## ⚛️ Next.js & React Native Conventions
*   **Strict Typing:** All components must be strictly typed with TypeScript. Do not use `any`.
*   **UI/UX:** Prioritize a nice, clean, and clear UI/UX. Use whitespace effectively and ensure skeleton loaders or loading states are implemented for any data fetching.
*   **Validation:** Run `npm run lint` and `npm run build` frequently during frontend development to catch hydration or type mismatch errors early.
