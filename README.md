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
  - [x] §5 minimal admin bootstrap + seed script (`uv run python -m app.devtools.seed_demo`)
  - [x] §6.1–6.4 upload → lesson → attach → (stubbed) processing → publish → fan-out
  - [x] §6.5 Student Home / My Learning / lesson progress
  - [x] §14 Slice 1 golden path + failure injections green
  - [x] Migration 021 (questions ↔ generation link + answer_spec) applied to Supabase
  - [x] v1.3: publish a published lesson to more sections; retry / remove a failed file
  - [x] Real AI model behind `ContentAI` / `GradingAI` (Claude), real Supabase Storage behind `StorageGateway` — opt-in via env, see `docs/22d_*`
- [x] **Slice 2** — learning → mastery → spaced review (§7), see `docs/22c_*`
  - [x] focus cycles, finish, prerequisites; mastery check with hint → retry → explanation → verification
  - [x] async free-text grading (TX1/TX2), MasteryEngine, teacher review queue (confirm/adjust)
  - [x] fixed-cadence spaced review on the shared pipeline; reviews on Student Home
  - [x] parent/self learning: scoped mastery, opt-in spaced review (Oct 5 decision)
  - [x] Migration 022 applied to Supabase (2026-10-20)
- [x] **Extensions** — see `docs/22d_*`
  - [x] Teacher feedback: AI draft → teacher edit → publish → student notification
  - [x] Deliverable submissions (SUBMISSION_REQUIRED / BOTH), late labelling, resubmission history, teacher queue
  - [x] Parent and self assignments (create → prepare → preview → assign); guardian linking
  - [x] Video/audio transcription, YouTube via provided transcript, Word (.docx) — see `docs/22e_*`
  - [x] Marks on submissions (optional, teacher-only or student-visible, released by the teacher)
  - [x] Parents/students can change files until the work is assigned
  - [x] Independent 18+ self-learners (private personal space)
  - [x] Migration 023 applied to Supabase (2026-10-26)

## Demo data

```bash
./scripts/local_db.sh                       # fresh local DB from supabase/migrations
cd backend && AAYRA_BOOTSTRAP_KEY=local-key uv run python -m app.devtools.seed_demo
```

Creates *Aayra Demo High School → 2026-27 → Grade 10 → 10A* with Biology and Mathematics,
Sarah Johnson (Class Teacher + Primary Biology), Ravi Kumar (Primary Maths), Priya Nair
(Biology co-teacher) and 28 students — all through the public API.

## Background worker

Content processing, lesson preparation, assignment fan-out and scheduled releases run as jobs:

```bash
cd backend && uv run python -m app.worker
```

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
