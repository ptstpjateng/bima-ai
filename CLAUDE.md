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

## ⚛️ Next.js & React Native Conventions
*   **Strict Typing:** All components must be strictly typed with TypeScript. Do not use `any`.
*   **UI/UX:** Prioritize a nice, clean, and clear UI/UX. Use whitespace effectively and ensure skeleton loaders or loading states are implemented for any data fetching.
*   **Validation:** Run `npm run lint` and `npm run build` frequently during frontend development to catch hydration or type mismatch errors early.
