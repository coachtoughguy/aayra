# 22c — Slice 2 implementation notes and the source-scoped mastery decision (October 5, 2026)

Companion to **22 Aayra API & Event Contract v1.2** and **22b (v1.3 changes)**. Where this note and 22 differ, this note wins.

## 1. Decision (LOCKED, Vinay, 2026-10-05): parent and self-learning are separate from school learning

- **Kept apart:** work assigned by a parent, or chosen by the student for self-learning, is treated separately from school-assigned work.
- **Review choice:** when a parent or self-learner assigns work, they choose either **spaced reviews** (Day 2 → 4 → 8 → 16 …, the same fixed cadence as school) or **one-time learning** (no reviews).
- **Mastery stays in its own scope:** mastery earned in parent or self work only counts for that work. It **never adds to or changes mastery for school-assigned learning**, even when the concepts are the same.
- **School work:** always uses spaced review and cannot opt out. This is enforced in the database.

### How it is implemented

| Source | Creates mastery evidence | Spaced reviews | Mastery scope |
|---|---|---|---|
| SCHOOL | yes | always | `SCHOOL` |
| PARENT | yes | only if `assignments.review_mode = 'SPACED'` | `PARENT` |
| SELF | yes | only if `assignments.review_mode = 'SPACED'` | `SELF` |

- **Seed values:** `assignment_source_policies` holds the defaults above. They were previously seeded with PARENT/SELF unable to create mastery evidence or schedule reviews; migration 022 updates them.
- **Frozen scope:** every `evidence_events` row stores `source_scope`, `mastery_eligible` and `review_eligible`, all computed once when it is written. A later policy change never reinterprets old evidence.
- **Separate mastery rows:** `student_concept_mastery` is unique per (student, concept, lesson, `mastery_scope`). MasteryEngine reads only evidence from the same scope, so a parent lesson on "Photosynthesis" can never move school mastery for the same concepts.
- **Creating parent/self assignments:** built in the next increment, see `docs/22d_Feedback_Submissions_Family_Learning_and_Real_AI.md`.

## 2. Migration 022 (applied to Supabase 2026-10-20, version 20261021050125)

`022_source_scoped_mastery_and_review_mode`:
1. Upserts `assignment_source_policies` to the values above.
2. Adds `assignments.review_mode` (`SPACED` / `ONE_TIME`, default `SPACED`) and the check `assignments_school_always_spaced`.
3. Adds `evidence_events.source_scope` and `student_concept_mastery.mastery_scope`. It also replaces the mastery unique key with one that includes the scope.
4. Adds `student_question_exposures.assessment_attempt_id`. This links each served question to its attempt, so the server knows which question is open.

All changes are additive and the database is empty.

## 3. Slice 2 behaviour worth knowing (contract §7)

- **Focus cycles:** the duration is computed by the server and capped at 4 hours. `client_reported_seconds` is stored as telemetry only.
- **Prerequisites:** a lesson with an unmet dependency cannot be started (`409 PREREQUISITE_NOT_COMPLETED`, which lists the missing lessons).
- **Mastery check:**
  - **Fixed questions:** the blueprint and its questions are frozen at the start of the attempt.
  - **Exposure:** a question counts as seen when it is served, not when it is answered.
  - **Fresh questions only:** the mastery check never reuses a question the student has already seen.
  - **Length:** at most 8 questions. Each concept needs `min_correct` questions, plus one verification question after a miss.
- **Wrong-answer repair (MCQ):**
  1. First wrong answer: the student gets a hint and one retry on the same question (evidence `help_level = HINT`).
  2. Second wrong answer: the student gets a short explanation.
  3. The next question favours a verification question on the same concept.
- **Free-text answers:** accepted with `202 PENDING` and graded by a `RESPONSE_GRADING` job; the next question is served at once.
  - **Waiting on grading:** when nothing is left to ask but grading is still pending, the attempt is `AWAITING_EVALUATION`. It completes when the last evaluation lands.
  - **Low confidence:** confidence below 0.6 sends the item to the teacher queue as `LOW_CONFIDENCE`.
  - **Model failure:** after 3 tries the item goes to the teacher as `MANUAL_REVIEW`, so a stuck answer always reaches a person.
- **Mastery rule:**
  - **Confidence:** the mean of (CORRECT = 1, PARTIAL = 0.5, INCORRECT = 0) × help factor (none 1.0, hint 0.75, explanation 0.5).
  - **MASTERED:** needs `min_correct` correct answers without help, and confidence ≥ 0.75.
  - **DEVELOPING:** confidence ≥ 0.4. Anything lower is NEEDS_REVIEW.
  - **Adjusted evaluations:** once replaced by a teacher's evaluation, they no longer count.
- **Teacher review queue:**
  - **Endpoints:** `GET /v1/teacher/review-items`, `POST /v1/teacher-review-items/{id}/confirm`, and `POST /v1/teacher-review-items/{id}/adjust`.
  - **Adjust:** writes a new TEACHER evaluation and keeps the AI one in history. It also adds new `TEACHER_EVALUATION` evidence and recomputes mastery.
  - **Access:** the teacher's authority is re-checked on every call.
- **Spaced review:**
  - **Scheduling:** finishing the check schedules a Day 2 review for each review-eligible concept.
  - **Fixed cadence:** the next review is always day × 2 from the anchor date, whatever the result, and stops at the end of the academic year.
  - **Shared pipeline:** reviews reuse the attempt machinery (`review_session_id`) and record `RETENTION` evidence.
  - **Reused questions:** a review may reuse questions the student has seen before (`is_anchor_reuse`).
  - **Before the due date:** starting a review early returns `409 REVIEW_NOT_DUE`.
- **Student Home:** due reviews appear in the `REVIEW_DUE_TODAY` tier of What's Next, and `my_reviews.due_today_count` is live.

## 4. Not yet built (next candidates)

- **Teacher feedback:** draft and publish (`teacher_feedback`), plus the student notification and deep link.
- **Deliverable submissions:** for assignments with `completion_mode` `SUBMISSION_REQUIRED` or `BOTH`.
- **Real models:** a real grading model behind `GradingAI`, and a real content model behind `ContentAI`.
- **Parent and self assignment creation:** the §9 API inventory.
