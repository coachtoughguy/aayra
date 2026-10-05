# 22b — Aayra API & Event Contract: v1.3 changes (October 5, 2026)

Companion to **22 Aayra API & Event Contract v1.2**. Where this note and 22 differ, this note wins.
All three changes were decided by Vinay on 2026-10-05 after Slice 1 was built.

## 1. Publish an already-published lesson to another section (changes §6.4)

**Problem:** v1.2 allowed publish only from `READY`, so a lesson could be published exactly once. A teacher who teaches 10A and 10B could not give both sections the same lesson.

**Decision (LOCKED):**
- `POST /v1/lessons/{id}/publish` accepts lessons in `READY`, `PUBLISHED` or `ACTIVE`.
- First publish (`READY → PUBLISHED`) records `approved_by` / `approved_at` / `published_at`. Later publishes keep that original approval and only create a new assignment for the new target.
- `reviewed_generation_version` must still equal the live generation (published content is locked, so it cannot change after the first publish).
- `version` is still required and still increments on every publish, so a double-tap with a new key gets `409 VERSION_CONFLICT`.
- New error `409 ALREADY_PUBLISHED_TO_TARGET` (details: existing `assignment_id`) when the lesson already has a non-closed assignment for that exact section or student.

## 2. Retry a file whose processing failed (new endpoint)

`POST /v1/content-items/{id}/retry-processing` — Actor: any active TEACHER in the item's school. Idempotency-Key required.
- Allowed only when the file's latest extraction is `FAILED`; otherwise `409 RETRY_NOT_ALLOWED` (details: current `processing_status`).
- Enqueues a fresh extraction job and moves every lesson using the file from `PROCESSING_FAILED` back to `PROCESSING` (lesson `version` +1).
- Event: `ContentProcessingRetried { content_item_id, lesson_ids }`.
- If the retry fails again, those lessons return to `PROCESSING_FAILED`.

## 3. Remove a file from an unpublished lesson (new endpoint)

`DELETE /v1/lessons/{id}/content/{content_item_id}` — Actor: teacher with authority over the lesson's subject. Idempotency-Key required.
- Only while the lesson is `DRAFT` / `PROCESSING` / `PROCESSING_FAILED` / `READY`; published content → `409 LESSON_CONTENT_LOCKED`.
- With files remaining: lesson → `PROCESSING` and preparation re-runs, producing a new generation version. With none left: lesson → `DRAFT`.
- Event: `LessonContentDetached`.
- Needed because retry alone cannot fix a file that is permanently unreadable.

## Related pipeline rule

Lesson preparation now marks the lesson `PROCESSING_FAILED` whenever any attached file's latest extraction failed. This includes attaching a file that had already failed in another lesson, so a lesson can never sit in `PROCESSING` forever.

## Schema

Migration **021_question_generation_link** was applied to Supabase on 2026-10-05 (version `20261005134011`). It adds `questions.blueprint_id` (FK to the generation that produced the question) and `questions.answer_spec` (options / answer key / rubric, never sent to students). No other schema changes in v1.3.
