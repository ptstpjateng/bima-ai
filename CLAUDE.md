# BIMA-AI Global Project Rules

## 🎯 Project Context
We are building BIMA-AI, a Hackathon project for DPMPTSP to unravel OSS RBA bureaucracy.
*   **Pillar 1:** Python/FastAPI + ChromaDB (AI Engine & RAG Pipeline)
*   **Pillar 2 & 3:** Next.js + React Native (Licensing Wizard & Super App UI)
*   **Core Backend:** Laravel 13 + Filament v.4 + PostgreSQL (TALL Stack)

## 🛠️ Core Operating Principles
1.  **Verify Before Moving On:** Never write massive blocks of code without testing. After creating or modifying any component (frontend or backend), you MUST run the appropriate build, lint, or test command to verify it works before proceeding to the next step.
2.  **Batch Full-Stack Scaffolding:** When asked to build a feature, consider the entire stack. For example, if building a CRUD feature, handle the migration, model, API controller, and admin resource simultaneously to ensure data consistency.
3.  **Leverage Sub-Agents:** If you encounter a framework-specific error (like a Next.js App Router issue or a deep Laravel exception), spawn a sub-agent to explore the documentation or internal files before blindly writing a fix.

## 🐘 Laravel & Filament Strict Conventions
*   **Authentication & Seeders:** When creating Laravel authentication or user seeders, **never hash passwords that are already being hashed** by model casts or mutators. Always check the `User` model for `$casts` with 'hashed' or `setPasswordAttribute` mutators before writing password logic. (This prevents the double-hashing bug).
*   **Filament Resources:** After generating or modifying Filament resources, you must run `php artisan filament:check` or attempt to load the admin panel to verify no type errors or property mismatches (like `$navigationGroup`) exist. Use string types, not enums, for `$navigationGroup` unless explicitly configured otherwise.
*   **Database Migrations:** Always verify database connectivity with `php artisan migrate:status` before proceeding with new migrations or seeders.

## 🖥️ VPS & Infrastructure

### SSH Access
*   **SSH Command:** `ssh bima-vps` → `wdnsds@116.254.113.81:2222` using `~/.ssh/id_bima_vps`
*   **Project directory:** `~/bima-ai` on VPS
*   **Deploy:** `ssh bima-vps "cd ~/bima-ai && docker compose pull && docker compose up -d --remove-orphans"`

### Service Map
| Service | Internal | Public | Notes |
|---|---|---|---|
| **nginx** | — | `:80` | Sole public entry point |
| **backend** (FrankenPHP) | `backend:80` | `:8000` (direct debug) | Laravel 13 + Filament |
| **ai-engine** (FastAPI) | `ai-engine:8000` | via `/webhook/` only | ChromaDB embedded |
| **postgres** | `postgres:5432` | — | internal only |
| **redis** | `redis:6379` | — | internal only |
| **frontend** | — | Vercel | decoupled, git auto-deploy |

### Nginx Routing (port 80)
*   `/webhook/` → `ai-engine:8000`
*   `/api`, `/sanctum`, `/admin`, `/livewire`, `/css`, `/js`, `/fonts`, `/storage` → `backend:80`
*   `/` → `302 /admin`

### URLs
*   **Filament Admin:** `http://116.254.113.81/admin` ✅ styled & confirmed
*   **Backend direct:** `http://116.254.113.81:8000`
*   **Frontend:** Vercel (auto-deploy on push to `main`, repo `ptstpjateng/bima-ai`, root dir `frontend/`)

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
| `FRONTEND_URL` | — | Vercel URL (update after deploy) |
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

### Frontend — `frontend/.env.local` (local) / Vercel dashboard (production)
| Key | Local | Production |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://backend-tall.test` | `http://116.254.113.81` |

> Set production env vars in **Vercel Dashboard → Project → Settings → Environment Variables**. Never commit `.env.local`.

## ⚛️ Next.js & React Native Conventions
*   **Strict Typing:** All components must be strictly typed with TypeScript. Do not use `any`.
*   **UI/UX:** Prioritize a nice, clean, and clear UI/UX. Use whitespace effectively and ensure skeleton loaders or loading states are implemented for any data fetching.
*   **Validation:** Run `npm run lint` and `npm run build` frequently during frontend development to catch hydration or type mismatch errors early.
