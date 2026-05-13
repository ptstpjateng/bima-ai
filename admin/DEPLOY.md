# Deploying admin.nolongin.com to Vercel

The admin frontend lives in this directory and deploys to Vercel as a SEPARATE
project from the user-facing portal (which is at frontend/).

## One-time setup

1. **Vercel project**
   - vercel.com → Add New Project → Import from `ptstpjateng/bima-ai`
   - Root Directory: `admin/` (NOT the repo root)
   - Framework Preset: Next.js (auto-detected)
   - Build & Development Settings: defaults

2. **Custom domain**
   - Vercel project → Settings → Domains → Add `admin.nolongin.com`
   - Vercel will give you a CNAME target (e.g., `cname.vercel-dns.com`)

3. **DNS at Hostinger**
   - Hostinger → DNS Zone Editor for `nolongin.com`
   - Add CNAME record: `admin` → `<vercel-cname-target>` TTL 300
   - Wait for propagation (~1 min)

4. **Environment variables (Vercel project → Settings → Environment Variables)**
   - `NEXT_PUBLIC_ADMIN_API_URL` = `https://nolongin.com/admin-api` (production)
   - `NEXTAUTH_SECRET` = output of `openssl rand -base64 32` (Production scope)
   - Apply to all environments OR Production only (your call)

5. **First deploy**
   - Push to `main` → Vercel auto-deploys
   - Visit https://admin.nolongin.com — should serve the login page

## Per-PR previews

By default Vercel builds preview URLs on every PR (e.g., `admin-pr-N-bima-ai.vercel.app`).
This is great for testing the new admin without touching production. No setup needed.
Preview env vars inherit from the Vercel project unless you override per-environment.

## Rollback

Vercel → Project → Deployments → click any past deployment → "Promote to Production".
