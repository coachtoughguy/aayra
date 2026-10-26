# 22e — Video/Audio, YouTube, Word Documents, Marks and Independent Learners

Status: built and tested (111 tests green), 2026-10-20. Builds on 22d.
Migration 023 is **applied** to Supabase (2026-10-26, version `20261026020324`).

## Decisions taken (Oct 20, Vinay)

| Topic | Decision |
|---|---|
| Speech-to-text | OpenAI transcription API, behind a swappable interface with a deterministic stand-in. |
| YouTube | The uploader provides the transcript (paste, or the text of a .vtt/.srt file). We never download YouTube media. The official Data API is optional and is used only for title, duration and an existence check. |
| Adult self-learners without a school | A private **personal space** stored as a `schools` row with `school_kind = INDEPENDENT`. |
| Family assignment files | Can be added or removed **only before assigning**. After that the content is locked, the same as any active assignment. |
| Marks | Already decided in doc 12: optional per assignment; teacher-only or student-visible. |

## 1. How each content type becomes evidence (`app/ai/sources.py`)

| Type | Path | Chunks |
|---|---|---|
| VIDEO, AUDIO | ffmpeg converts the file to mono 16 kHz MP3 pieces of 10 minutes each → OpenAI transcription → segments | `TRANSCRIPT` with start/end seconds |
| YOUTUBE | provided transcript (WebVTT, SRT, or pasted text with or without `m:ss` stamps) | `TRANSCRIPT` (timestamps when present) |
| DOCUMENT (.docx) | parsed on the server, with no model call: headings start new chunks, page breaks are tracked, tables kept | `DOCUMENT_TEXT`, `TABLE` |
| PDF, IMAGE/PHOTO, WEB_LINK | content model (Claude), as before | as before |

Details:
- **Media limits:**
  - Media longer than `AAYRA_MAX_MEDIA_SECONDS` (4 h by default) fails with `MEDIA_TOO_LONG`.
  - No audio track: `NO_AUDIO_TRACK`. No recognisable speech: `NO_SPEECH_FOUND`.
  - Without ffmpeg on the server, only files already under 25 MB and in a format the API accepts are sent. Others fail with `MEDIA_TOO_LARGE` / `UNSUPPORTED_MEDIA_FORMAT`.
- **Timestamps:** the `whisper-1` model returns per-segment timestamps. The `gpt-4o-*-transcribe` models return text only, so each 10-minute piece becomes one timed segment.
- **Saved details:** the duration and language found by transcription are saved on `content_items` if they were empty.
- **Transcription outages:** a provider outage (429/5xx/timeout) is retried by the worker. **New:** after the final retry the file is marked `FAILED` (`PROVIDER_UNAVAILABLE`) and its lessons become `PROCESSING_FAILED`, so the uploader can retry. Before this change, lessons stayed in PROCESSING forever.
- **Logging:** every transcription call is logged in `ai_invocations` (model name, prompt version `openai-stt-v1`). Parsing a .docx or a provided transcript isn't a model call, so it isn't logged.
- **Unsupported Word files:** old `.doc` files and non-Word files fail with `UNSUPPORTED_DOCUMENT_FORMAT` and the message "Save it as .docx or PDF".

### YouTube API changes

- `POST /v1/content-items` with `content_type: YOUTUBE`:
  - `source_url` must be a real YouTube video link (watch, youtu.be, shorts, embed); otherwise it returns 422.
  - It accepts an optional `transcript_text`. Without it, the item is `upload_status: AWAITING_TRANSCRIPT` and can't be attached (`409 CONTENT_NOT_UPLOADED`).
- `POST /v1/content-items/{id}/transcript {text}` (uploader only):
  - Stores the transcript and starts processing.
  - It can be sent again only to replace a transcript whose processing failed. Otherwise it returns `409 TRANSCRIPT_ALREADY_PROVIDED`.

## 2. Marks on submissions (doc 12)

- **Publishing:** `POST /v1/lessons/{id}/publish` accepts `max_marks` (optional, > 0) and `marks_visibility` (`TEACHER_ONLY` by default, or `STUDENT_VISIBLE`). Marks are only for `SUBMISSION_REQUIRED` / `BOTH`; otherwise 422. The database enforces this too.
- **`PUT /v1/submissions/{id}/marks {marks_awarded}`:**
  - Allowed for a teacher with authority over the lesson. The mark can't exceed `max_marks`, and returns `409 MARKS_NOT_ENABLED` if the assignment isn't marked.
  - Marking closes the submission's "Needs Your Attention" item.
  - Teachers can correct a mark, even after release. Every change is audited (`SUBMISSION_MARKED`), and a correction after release notifies the student ("Updated mark").
- **Releasing:**
  - `POST /v1/submissions/{id}/marks/release` releases one mark. `POST /v1/assignments/{id}/marks/release` releases every marked submission at once.
  - Releasing works only for `STUDENT_VISIBLE` assignments; `TEACHER_ONLY` returns `409 MARKS_TEACHER_ONLY`. A release sends a `MARKS_RELEASED` notification.
- **What students see:** students see `{awarded, out_of}` only when the mark is released **and** the assignment is student-visible. They never see averages, rank or other students' marks.
- **What teachers see:** the teacher's submissions list includes the marks, the visibility setting and the release state.
- **Mastery:** marks never create or change mastery. Late work keeps the teacher's mark, with no automatic deduction.

## 3. Changing files on parent/self assignments

- **Adding a file:** `POST /v1/{parent|self}-assignments/{id}/content {content_item_id, version}`. It must be the creator's own uploaded file (and a YouTube link needs its transcript). Up to 10 files.
- **Removing a file:** `DELETE /v1/{parent|self}-assignments/{id}/content/{content_item_id}?version=N`. At least one file must stay (`409 LAST_FILE`).
- **After a change:** the lesson is prepared again (status `PREPARING`). The assignment version increases, and a stale version returns `409 VERSION_CONFLICT`.
- **After assigning:** both endpoints return `409 CONTENT_LOCKED` ("create a new assignment").

## 4. Independent adult self-learners (docs 02 §3.4, 05 §18.5)

- **`POST /v1/independent-learners`:**
  - **Who can call it:** any verified login; the user doesn't need an Aayra account yet.
  - **Request body:** `display_name`, `date_of_birth`, `confirms_18_or_older`, `country_code`, `timezone`, optional `preferred_language` and `subjects`.
  - **Under 18:** returns `403 AGE_RESTRICTED` with `route: SCHOOL_AFFILIATION`.
  - **What it creates:** the user (if new) and an INDEPENDENT space (one open-ended "Personal" year of 10 years, one grade, one section). It also creates a student profile, the STUDENT role, an ACTIVE enrollment and the chosen subjects.
  - **Records:** sets `users.age_gate_status = self_declared_18_plus`, writes the audit entry `INDEPENDENT_LEARNER_SIGNED_UP` and emits `IndependentLearnerSignedUp`.
  - **Existing accounts:** a teacher or parent account can also become a self-learner (one person, one account). Anyone who already has a student profile, at a school or independent, gets `409 ALREADY_A_STUDENT`. An email already used by another account returns `409 EMAIL_IN_USE`.
- **`GET /v1/independent-learners/me`** and **`POST /v1/independent-learners/me/subjects`**: a duplicate subject returns `409 SUBJECT_EXISTS`. A school student calling these gets `409 NOT_INDEPENDENT`.
- **`GET /v1/students/me/subjects`** (any student): lists the subjects to choose from when creating self-learning.
- **Everything else** uses the existing self-learning endpoints unchanged. Mastery is `SELF`-scoped and spaced review is opt-in. There are no teachers or guardians in a personal space, and nobody from a school can see into it.

## 5. Migration 023 (`023_marks_and_independent_learners`, applied 2026-10-26)

1. Adds `assignments.max_marks` and `marks_visibility`, with checks (positive marks; marks only on submission modes).
2. Adds `assignment_submissions.marks_awarded`, `marked_by`, `marked_at` and `marks_released_at`, with checks.
3. Adds `schools.school_kind` (`SCHOOL` / `INDEPENDENT`) and `owner_user_id`, with a check. A unique index allows one independent space per user.
4. Adds `self_declared_18_plus` to `users.age_gate_status` (the constraint name was confirmed on the live schema).

All changes are additive.

## 6. Configuration

| Variable | Default | Notes |
|---|---|---|
| `AAYRA_TRANSCRIPTION_PROVIDER` | `stub` | Set to `openai` to turn on real transcription. |
| `AAYRA_OPENAI_API_KEY` | — | Server-side only. |
| `AAYRA_TRANSCRIPTION_MODEL` | — | **Required** with `openai`, and there is deliberately no default (for example `whisper-1` for timestamps). |
| `AAYRA_TRANSCRIPTION_TIMEOUT_SECONDS` | 300 | |
| `AAYRA_MAX_MEDIA_SECONDS` | 14400 | |
| `AAYRA_YOUTUBE_API_KEY` | — | Optional; used for metadata only. |

The server needs `ffmpeg`/`ffprobe` installed for large or video files.

## 7. Still open

1. **Age check:** the age-verification mechanism for direct signup is still OPEN in doc 02. A self-declared date of birth is recorded until it is decided.
2. **Wrong path for school students:** a school student who later wants an independent space too isn't supported, because one student profile per user is enforced by the schema.
3. **Media handling:** large-media processing time and cost budgets, and whether to keep the audio pieces, need a decision before turning on real transcription for everyone.
4. **Parents and marks:** marks aren't shown to parents. The school-work view for parents isn't built yet.
