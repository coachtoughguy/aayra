# Aayra

AI-powered Learning OS. Product and technical design live in the claude.ai **aayra** project
(docs 00–22). The implementation contract is **22 — Aayra API & Event Contract v1.2**.

## Layout

| Path | What |
|---|---|
| `supabase/migrations/` | Exact SQL of every migration live in Supabase project `aayra` (source of truth for the schema) |
| `supabase/local/` | Tiny Supabase-compat shim (roles, `auth.uid()`) so plain Postgres can replay the migrations for tests |
| `backend/` | FastAPI service (Python 3.12, SQLAlchemy Core + asyncpg) |
| `scripts/` | Dev helpers |

## Build order (doc 21, Oct 4 addendum)

- [x] Contract frozen (v1.2)
- [x] Migration 020 applied to Supabase
- [ ] **Slice 1** — bootstrap (§5) → content/lesson/publish (§6) → Student Home
  - [x] Shared plumbing: auth + tenancy, error envelope, idempotency, optimistic concurrency, outbox, request context
  - [ ] §5 minimal admin bootstrap + seed script
  - [ ] §6.1–6.4 upload → lesson → attach → (stubbed) processing → publish → fan-out
  - [ ] §6.5 Student Home / My Learning / lesson progress
  - [ ] §14 Slice 1 golden path + failure injections green
- [ ] **Slice 2** — learning → mastery → spaced review (§7)

## Running tests

Tests need a Postgres 16+ server; each test gets a fresh database cloned from a template built
from `supabase/migrations/`.

```bash
cd backend
uv sync
AAYRA_TEST_PGHOST=localhost AAYRA_TEST_PGPORT=5432 AAYRA_TEST_PGPASSWORD=postgres uv run pytest
```

CI (`.github/workflows/backend.yml`) runs lint + tests against Postgres 17 on every push.

## Conventions the code enforces

- All business writes go through FastAPI in explicit transactions; RLS is defense-in-depth.
- State change + outbox event + idempotency record commit together (`app/idempotency.py`, `app/outbox.py`).
- Writes to versioned rows use `update_versioned` → `409 VERSION_CONFLICT` on stale versions.
- Errors are `{ "error": { code, message, details } }`; clients branch on `code` (contract §13).
- Schema changes: add a new migration to Supabase *and* commit its SQL here; never edit an applied one.
