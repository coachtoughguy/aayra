-- =====================================================================
-- 010_teacher_review_feedback.sql
-- Domain: Teacher Review Queue & Feedback (Physical Data Model §11)
-- Tables: teacher_review_items, evaluation_adjustment, teacher_feedback
-- An AI-drafted feedback comment is explicitly never auto-published,
-- it must be a teacher action. Wireframe correction: buttons read
-- "Confirm Evaluation" / "Adjust Evaluation", not "Approve" / "Edit
-- grade", because this queue is AI evidence evaluation, not formal
-- grading.
-- =====================================================================

CREATE TABLE teacher_review_items (
  review_item_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id                 UUID NOT NULL REFERENCES schools(school_id),
  teacher_id                  UUID NOT NULL REFERENCES teacher_profiles(teacher_id),
  student_id                    UUID NOT NULL REFERENCES student_profiles(student_id),
  assignment_id                   UUID REFERENCES assignments(assignment_id),
  response_evaluation_id            UUID REFERENCES response_evaluations(response_evaluation_id),
  submission_id                       UUID REFERENCES assignment_submissions(submission_id),
  reason_type                          VARCHAR(30) NOT NULL
                                          CHECK (reason_type IN ('LOW_CONFIDENCE','SUBJECTIVE_RESPONSE','TEACHER_REQUIRED','MANUAL_REVIEW')),
  status                                VARCHAR(20) NOT NULL DEFAULT 'PENDING'
                                          CHECK (status IN ('PENDING','CONFIRMED','ADJUSTED','DISMISSED')),
  ai_evaluation_snapshot                  JSONB NOT NULL,   -- the AI's original read, frozen at queue time
  reviewed_at                              TIMESTAMPTZ,     -- populated automatically whenever a response_evaluations row is written by the AI with evaluator_confidence below threshold
  created_at                                TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_teacher_review_items_teacher_status ON teacher_review_items (teacher_id, status, created_at);

COMMENT ON TABLE teacher_review_items IS
  'This queue only ever surfaces evidence tied to the reviewing teacher''s own teacher_section_subject_assignments scope, joined through response_evaluations -> assessment_responses -> assessment_attempts -> lesson -> subject/section -> teacher_assignment, so a teacher never reviews evidence from classes they don''t teach.';

CREATE TABLE evaluation_adjustment (
  adjustment_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  review_item_id                   UUID UNIQUE NOT NULL REFERENCES teacher_review_items(review_item_id),
  original_response_evaluation_id    UUID NOT NULL REFERENCES response_evaluations(response_evaluation_id),   -- the AI's evaluation being adjusted
  new_response_evaluation_id           UUID NOT NULL REFERENCES response_evaluations(response_evaluation_id), -- the teacher's replacement row (is_current)
  adjusted_by                            UUID NOT NULL REFERENCES teacher_profiles(teacher_id),
  adjustment_note                          TEXT,
  adjusted_at                               TIMESTAMPTZ NOT NULL,
  created_at                                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE evaluation_adjustment IS
  'Present only when the teacher chooses "Adjust Evaluation"; "Confirm Evaluation" writes reviewed_at on teacher_review_items with no row here. Adjusting never rewrites original_response_evaluation_id in place -- it inserts a new response_evaluations row (evaluated_by = TEACHER, is_current = TRUE), flips the AI''s row to is_current = FALSE (§9), and a new append-only evidence_events row is written reflecting the corrected read. "Confirm Evaluation" leaves the originating response_evaluations/evidence_events/student_concept_mastery values untouched and just closes the queue item; "Adjust Evaluation" inserts a new response_evaluations row and a new evidence_events row (the ledger is append-only, §9) reflecting the teacher''s corrected read, which then drives student_concept_mastery''s recompute -- the AI''s original response evaluation and evidence event are never edited in place, only superseded.';

CREATE TABLE teacher_feedback (
  feedback_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  teacher_id           UUID NOT NULL REFERENCES teacher_profiles(teacher_id),
  student_id             UUID NOT NULL REFERENCES student_profiles(student_id),
  assignment_id             UUID NOT NULL REFERENCES assignments(assignment_id),
  submission_id               UUID REFERENCES assignment_submissions(submission_id),
  feedback_text                 TEXT,
  ai_draft_text                   TEXT,   -- never shown to the student directly; a draft the teacher may edit or discard
  status                           VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
                                      CHECK (status IN ('DRAFT','PUBLISHED')),
  visible_to_student                BOOLEAN NOT NULL DEFAULT FALSE,
  published_at                       TIMESTAMPTZ,
  created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_teacher_feedback_student_assignment ON teacher_feedback (student_id, assignment_id);

COMMENT ON TABLE teacher_feedback IS
  'teacher_feedback.ai_draft_text existing is not itself an action -- visible_to_student only flips true once status = PUBLISHED, enforcing the "AI draft is never automatically teacher feedback" rule at the schema level, not just in the UI.';
