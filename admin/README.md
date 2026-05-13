# BIMA Admin Console

Internal admin web app for **BIMA-AI** (DPMPTSP Jawa Tengah). Replaces the
Laravel/Filament admin under `backend-tall/` as part of the FastAPI migration.

Deploys to `admin.nolongin.com` as a **separate Vercel project** (decided in
`BIMA-Vault/Decisions.md` §7, hosting strategy in `BIMA-Vault/frontend.md`).
Keeping the admin bundle on its own Vercel project prevents the admin chunk
from leaking into the MSME portal at `app.nolongin.com`.

## Phase 1 scope

Shell only. No real data flows yet — admin-api endpoints land Phase 2.

- Brand tokens applied per `BIMA-Vault/Brand.md` ("Midnight Government")
- Sidebar + top bar shell
- Login page (POSTs to admin-api `/auth/login`)
- Dashboard with four placeholder stat tiles and two empty-state panels
- Middleware-enforced auth on every non-login route

## Stack

- **Next.js 16** App Router + Turbopack + TypeScript strict
- **Tailwind v4** (CSS-first config — see `src/app/globals.css`)
- **shadcn/ui** (Radix-base, Nova preset) — overridden palette via Brand tokens
- **Jost** (display) + **Work Sans** (body) via `next/font/google`
- **Framer Motion** for page transitions + dashboard count-up
- **TanStack Query** for data fetching (Phase 2 will populate)
- **react-hook-form** + **zod** for validated forms
- **Sonner** for toasts
- **lucide-react** icons

## Local development

```bash
cp .env.example .env.local
# Edit .env.local — set NEXT_PUBLIC_ADMIN_API_URL to your admin-api dev URL.
npm install
npm run dev
```

Open <http://localhost:3000>. Unauthenticated requests redirect to `/login`.

> **Note:** during Phase 1 the login POST returns 404 because admin-api isn't
> deployed yet. That's expected — the shell is built first, the API contract
> lands in Phase 2.

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Next.js dev server with Turbopack |
| `npm run build` | Production build (run before every commit) |
| `npm run start` | Run the production build locally |
| `npm run lint` | ESLint via `eslint-config-next` |

## Environment variables

| Var | Required | Notes |
|---|---|---|
| `NEXT_PUBLIC_ADMIN_API_URL` | yes | Base URL for admin-api auth endpoints |
| `NEXTAUTH_SECRET` | Phase 2 | Reserved — server-side session signing |

## Project layout

```
admin/
├── src/
│   ├── app/
│   │   ├── (admin)/             Protected admin shell (sidebar + topbar)
│   │   │   ├── layout.tsx
│   │   │   └── dashboard/page.tsx
│   │   ├── api/auth/set-token/  httpOnly cookie writer for the JWT
│   │   ├── login/page.tsx       Public login page
│   │   ├── layout.tsx           Root — fonts + Providers
│   │   ├── providers.tsx        TanStack Query + Tooltip + Toaster
│   │   ├── globals.css          Tailwind v4 + Brand tokens + shadcn vars
│   │   └── page.tsx             Redirects to /dashboard
│   ├── components/
│   │   ├── dashboard/stat-tile.tsx
│   │   ├── layout/sidebar.tsx
│   │   ├── layout/topbar.tsx
│   │   └── ui/                  shadcn primitives
│   ├── lib/utils.ts             cn() helper
│   └── proxy.ts                 Next.js 16 proxy (replaces middleware) — auth gate
└── ...
```

## Deployment

**Do not deploy yet.** Sprint B.2 will wire the Vercel project + DNS for
`admin.nolongin.com`. The build must already succeed via `npm run build` —
that's enforced before every commit.

## What's NOT in Phase 1

- Resource pages (User Stats, AI Interactions thread viewer, Ingestion Sources,
  KBLI list, User CRUD) → Phase 2
- Server-side JWT validation (middleware only checks cookie presence today —
  the real `/auth/me` validation lands when admin-api is live)
- NextAuth provider wiring
- File upload UI (MinIO presigned PUT) → Phase 2
