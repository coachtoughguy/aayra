# 22f — Logins, Deployment, Teacher Editing, Progress & Tutor, Admin Operations, Web App

Status: built and tested on 2026-11-01. There are 138 backend tests, and a scripted browser walkthrough of every role was run against a local API.
Hosting is Vercel (API and web app) plus Supabase. Deployment steps are in `DEPLOY.md`.

## 1. Real logins (Supabase Auth)

- **Token checks:**
  - The API accepts Supabase access tokens signed either with the project's **signing keys** (ES256/RS256, checked against `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`, issuer checked) or with the **legacy shared secret** (HS256).
  - In `production` the public development secret is never accepted.
- **Creating accounts:** this goes through the Supabase Auth Admin API (`AAYRA_IDENTITY_PROVIDER=supabase`):
  - **Staff and parents with email** get an invite email. The link opens the web app, where they choose a password.
  - **Students without email** (doc 21 v1.0) get a **username** login, e.g. `demohs.10a001`. Supabase needs an email, so the username maps to `<username>@AAYRA_STUDENT_LOGIN_DOMAIN`.
    - The account is created with a **one-time temporary password**. It is shown to the admin once and never stored by Aayra.
    - `must_change_password` makes the web app ask for a new password at first sign-in (doc 21 Sept 9).
  - `POST /v1/students/{id}/reset-login` gives an admin a new temporary password for a username student.
- **`GET /v1/me`** now returns `schools`, `student` (school, class, kind) and `homes` (`STUDENT`, `INDEPENDENT`, `TEACHER`, `PARENT`, `ADMIN`). The web app picks screens from `homes`. A valid login without an Aayra account gets `401 NO_ACCOUNT`, and the app offers independent 18+ signup.

## 2. Deployment model (Vercel)

- **API:** one Python function; `backend/vercel.json` gives it a 300 s maximum.
  - **Database:** Supabase's transaction pooler (port 6543) is used. The API detects Vercel and switches to no client pool, with prepared-statement caching off.
- **Background jobs:** these are rows in Postgres claimed with `SKIP LOCKED`, so any number of runners is safe. They run in three ways:
  - `POST /v1/worker/kick`: the web app calls it after every change, merging calls that overlap. It runs for up to about 25 s, and screens refresh when jobs finish.
  - `GET|POST /internal/worker/run` with `AAYRA_WORKER_SECRET`: called **every 5 minutes by GitHub Actions** (`worker-tick.yml`), so scheduled releases and grading run when nobody is online. Vercel's daily cron is a last-resort backup.
  - `python -m app.worker`: if the API is ever hosted somewhere with an always-on process.
- **Other changes:**
  - **CORS** comes from `AAYRA_CORS_ORIGINS`.
  - **ffmpeg** ships with the app as a bundled binary (`imageio-ffmpeg`), because serverless hosts don't have it.
  - **Migration 024** creates the private `lesson-content` bucket.

## 3. Teacher review and editing before publishing (docs 11/12, LOCKED)

- **Edit endpoints:** questions, answer options or rubric, hints, explanations, difficulty, skill; add a question; remove a question; rename or re-weight a topic; remove a topic; regenerate.
- **Every edit makes a new generation:** a copy of the current one with the change applied. The AI's original stays in history, and publishing still names the exact `reviewed_generation_version`, so nothing unreviewed can be published. Each edit carries the version it was made against, which protects against two teachers editing at once.
- **Rules:**
  - Every topic keeps at least one question, and a lesson keeps at least one topic.
  - Renaming a topic never renames it in other lessons.
  - Editing works only while the lesson is READY; published content is locked.
- **Labels and audit:** questions are labelled `AI`, `EDITED` or `TEACHER`. Edits are audited (`LESSON_GENERATION_EDITED`).

## 4. Student progress, notifications and the AI tutor

- **`GET /v1/students/me/progress`:** pending and completed work by source, understanding by subject (school and own learning shown separately), reviews in the next 7 days, this week vs last week (focus minutes, lessons finished, checks, reviews), and a simple streak.
  - It never shows rank, percentiles, comparisons with others, or internal confidence numbers.
  - Days and weeks use the school's (or personal space's) time zone.
- **Notifications inbox** (all roles): `GET /v1/me/notifications`, mark one read, mark all read.
- **In-lesson AI tutor** (`POST /v1/lessons/{id}/tutor`):
  - **Modes:** ask, explain simply, example, hint, quiz me.
  - **Grounded:** each turn sends the lesson's most relevant evidence sections. It refuses unrelated requests and never writes graded work.
  - **Paused during checks:** while a mastery check is running, it returns `409 TUTOR_PAUSED_DURING_CHECK`.
  - **Daily cap:** 80 messages per student per day.
  - **Private:** migration 025 adds tables only the backend can read. Parents and teachers never see conversations.
  - **Logging:** every reply is logged in `ai_invocations`.
  - **Voice:** the web app uses the browser's own dictation and read-aloud. Voice messages arrive as text, marked `VOICE`.

## 5. School Admin operations

- **Bulk student import (CSV):**
  - Every row is checked, with a reason per failure: missing field, duplicate admission id, duplicate in the file, unknown class, invalid date, under 13, invalid email.
  - Valid rows are then added even if other rows fail (doc 02 Sept 9). New logins and temporary passwords are shown once.
- **Transfer:** within the same year. History is kept, and in-progress work continues.
- **Withdrawal:** access is revoked; records are kept, never deleted.
- **Year rollover:** a dry-run preview first.
  - Each class maps by default to the next grade's class with the same letter, and the admin can change any mapping. The top grade "finishes".
  - Open assignments close with the year. Teacher assignments end. The old year becomes CLOSED (read-only history) and the new year becomes ACTIVE.
- **Admin home (operational only):** active students, teachers, and a Needs Attention list (students without a guardian, classes without a class teacher or primary subject teacher, pending invites, imports waiting).
- **Late-joiner back-fill (contract §11 / doc 21, Oct 4 LOCK; it was missing until now):** when a student becomes active in a class (new student, transfer or rollover), they get the class's current and future work.
  - Work already past due is not auto-assigned. Instead, the teacher gets a `LATE_JOINER_CATCH_UP` notification listing it.

## 6. Web app (`web/`, React + Vite + TypeScript)

- **Sign-in:** email or username plus password; first-login password setup; forgot password; 18+ independent signup ("Set up your learning space").
- **Student / independent learner:**
  - home (Continue learning, What's next, stats), My learning (with completed work), progress, learn something
  - lesson screen: material viewer for PDF, image, video, audio, YouTube, web link and extracted text; focus timer; tutor panel
  - mastery check (hint → retry → explanation), reviews, submissions with marks, teacher feedback, notifications
- **Teacher:** home, new lesson (upload files / YouTube with transcript / web page), review and edit before publishing, publish (class, due date, delayed release, completion mode, optional marks), assignments, per-assignment class list in alphabetical order (marks, release, AI-drafted feedback), Needs attention queue.
- **Parent:** children, child overview (school work plus work they assigned, never the child's self-learning), assign learning (spaced or one-time), and change files until assigned.
- **School admin:** home, classes and subjects (years, grades, classes, subjects), students (add, link parent, transfer, reset login, withdraw), teachers (invite, assign), CSV import, new-year rollover.
- **Design:** works on phones, supports a dark theme, and every action is keyboard-accessible. The local-only developer sign-in (`VITE_DEV_LOGIN`) is never available on the deployed site.

## 7. Migrations

| Migration | Status |
|---|---|
| 024 `storage_bucket` | Applied 2026-11-01 (version `20261101050501`). The private `lesson-content` bucket exists. |
| 025 `tutor_conversations` | Applied 2026-11-01 (version `20261101050502`). |

## 8. Still open / next

1. Real email delivery (custom SMTP) before inviting real schools.
2. Parent escalation flow (teacher-approved), the teacher calendar and reports, the principal views, and Learning DNA (later phase).
3. The age-verification mechanism for direct signup (doc 02 OPEN).
4. Turning on real AI and transcription needs keys, model ids and a cost budget.
