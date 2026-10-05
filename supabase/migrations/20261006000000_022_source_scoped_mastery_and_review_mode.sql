-- 022_source_scoped_mastery_and_review_mode
-- PENDING: not yet applied to the hosted project. Apply via Supabase (with Vinay's approval), then
-- rename this file to the version Supabase assigns.
--
-- Implements the Oct 5 product decision (doc 21 / 22c):
--   * Parent-assigned and self-chosen learning is separate from school learning.
--   * When a parent or a self-learner assigns work they choose SPACED reviews or ONE_TIME learning.
--   * Mastery earned in parent/self work is scoped to that work; it never adds to or changes
--     mastery for school-assigned learning.
-- Plus one gap found while implementing §7.4: question exposures must be linked to the attempt
-- that served them, so the server knows which question is open in an attempt.

-- 1. Source policies. Seeded earlier with PARENT/SELF unable to create mastery evidence or schedule
--    reviews; the Oct 5 decision changes both (mastery is scoped, reviews are opt-in per assignment).
INSERT INTO assignment_source_policies
  (source_type, recommendation_priority, can_gate_school_learning, can_create_mastery_evidence,
   can_schedule_retention_reviews)
VALUES
  ('SCHOOL', 1, true,  true, true),
  ('PARENT', 2, false, true, true),   -- reviews only when the assignment opts in (review_mode)
  ('SELF',   3, false, true, true)    -- reviews only when the assignment opts in (review_mode)
ON CONFLICT (source_type) DO UPDATE SET
  recommendation_priority        = EXCLUDED.recommendation_priority,
  can_gate_school_learning       = EXCLUDED.can_gate_school_learning,
  can_create_mastery_evidence    = EXCLUDED.can_create_mastery_evidence,
  can_schedule_retention_reviews = EXCLUDED.can_schedule_retention_reviews,
  updated_at                     = now();

-- 2. Per-assignment choice: spaced reviews or one-time learning. School work always uses spaced review.
ALTER TABLE assignments
  ADD COLUMN review_mode character varying NOT NULL DEFAULT 'SPACED'
    CONSTRAINT assignments_review_mode_check CHECK (review_mode IN ('SPACED', 'ONE_TIME')),
  ADD CONSTRAINT assignments_school_always_spaced CHECK (source_type <> 'SCHOOL' OR review_mode = 'SPACED');

-- 3. Mastery scope, frozen at write time on both the evidence and the mastery row.
ALTER TABLE evidence_events
  ADD COLUMN source_scope character varying NOT NULL DEFAULT 'SCHOOL'
    CONSTRAINT evidence_events_source_scope_check CHECK (source_scope IN ('SCHOOL', 'PARENT', 'SELF'));
ALTER TABLE student_concept_mastery
  ADD COLUMN mastery_scope character varying NOT NULL DEFAULT 'SCHOOL'
    CONSTRAINT student_concept_mastery_scope_check CHECK (mastery_scope IN ('SCHOOL', 'PARENT', 'SELF'));
-- The same lesson can be both school-assigned and parent/self-assigned; each scope keeps its own row.
ALTER TABLE student_concept_mastery
  DROP CONSTRAINT student_concept_mastery_student_id_concept_id_lesson_id_key,
  ADD CONSTRAINT student_concept_mastery_scope_key UNIQUE (student_id, concept_id, lesson_id, mastery_scope);
CREATE INDEX idx_student_concept_mastery_scope
  ON student_concept_mastery (student_id, mastery_scope, concept_id);

-- 4. Which attempt served each question.
ALTER TABLE student_question_exposures
  ADD COLUMN assessment_attempt_id uuid REFERENCES assessment_attempts(attempt_id);
CREATE INDEX idx_student_question_exposures_attempt
  ON student_question_exposures (assessment_attempt_id) WHERE assessment_attempt_id IS NOT NULL;
