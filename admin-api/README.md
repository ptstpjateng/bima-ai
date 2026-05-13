# BIMA admin-api

FastAPI service that will replace the Laravel/Filament admin panel for DPMPTSP
staff. Phase 1 ships only the scaffold — auth + health — so the rest of the
team can wire DevOps + Frontend in parallel. Business endpoints (users CRUD,
permits, KBLI, ingestion sources) land in Phase 2.

## Why a new service

See `BIMA-Vault/Decisions.md` §6 (drop Laravel) and `architect.md` §"Migration
Architecture". The short version: keep `ai-engine` single-worker (ChromaDB
embedded mode), put admin CRUD in a separately scalable FastAPI app on the
`bima-internal` network, share the existing Postgres database, and deprecate
Laravel/Filament once parity lands.

## Stack

- **FastAPI 0.115+** — HTTP framework.
- **SQLAlchemy 2.x async** with **asyncpg** — DB layer.
- **Alembic** — schema migrations (baseline marks Laravel tables as applied).
- **pydantic-settings** — env-driven config.
- **python-jose + passlib[bcrypt]** — JWT issuance + bcrypt-compat with
  Laravel's `users.password` column.

## Run locally

```bash
cd admin-api
cp .env.example .env  # then edit DATABASE_URL etc.
pip install -e .
uvicorn app.main:app --reload --port 8001
```

Then `curl http://localhost:8001/health` should return `{"status": "ok"}`.

## Run in Docker

The DevOps agent will add a `admin-api` service entry to root
`docker-compose.yml`. Until then:

```bash
docker build -t bima-admin-api admin-api/
docker run --rm -p 8001:8001 --env-file admin-api/.env bima-admin-api
```

## Database migrations

```bash
# inspect history
alembic history

# create a new migration
alembic revision -m "add foo"

# apply migrations
alembic upgrade head
```

The baseline migration (`alembic/versions/001_baseline.py`) marks every
existing Laravel-managed table as already-applied via `op.execute(...)` no-ops
and only creates the new `ingestion_sources` table.

## What lives here vs ai-engine

| Concern | admin-api | ai-engine |
|---|---|---|
| Admin CRUD endpoints | ✅ | — |
| LLM / RAG | — | ✅ |
| ChromaDB (embedded) | — | ✅ |
| Auth (admin login, JWT) | ✅ | — |
| WhatsApp/APTANA webhook | — | ✅ |
| Internal X-Internal-Key endpoints | partly (Phase 2) | partly (existing) |

## Project layout

```
admin-api/
├── alembic/               # migrations (Alembic)
├── app/
│   ├── main.py            # FastAPI app + middleware
│   ├── config.py          # pydantic-settings (env)
│   ├── db.py              # async engine + session factory
│   ├── deps.py            # FastAPI dependencies (DB, auth)
│   ├── models/            # SQLAlchemy 2 mapped classes
│   ├── schemas/           # Pydantic v2 request/response
│   └── routers/           # health, auth (Phase 1)
├── Dockerfile             # python:3.12-slim, non-root
├── pyproject.toml         # canonical dep list
├── requirements.txt       # mirror, used by Dockerfile
└── .env.example           # env template
```
