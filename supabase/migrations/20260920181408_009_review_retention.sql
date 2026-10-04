-- =====================================================================
-- 009_review_retention.sql
-- Domain: Review / Retention Engine (Physical Data Model §10)
-- Tables: review_schedules, review_schedule_items, review_sessions,
--         review_session_questions
-- Fixed power-of-two cadence, Day 2 -> 4 -> 8 -> 16 -> 32..., confirmed
-- by the Sept 16 addendum, superseding older "adaptive spacing"
-- language. Architecture-Locked (V3): every
-- review_schedule_items.scheduled_for = review_schedules.anchor_date +
-- day_offset(review_day), computed once from the single anchor_date at
-- schedule-creation time -- never chained from a prior item's
-- completed_at, and never recomputed off any date but anchor_date.
-- =====================================================================

CREATE TABLE review_schedules (
  review_schedule_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id             UUID NOT NULL REFERENCES student_profiles(student_id),
  lesson_id                UUID NOT NULL REFERENCES lessons(lesson_id),
  concept_id                 UUID NOT NULL REFERENCES concepts(concept_id),
  academic_year_id             UUID NOT NULL REFERENCES academic_years(academic_year_id),
  anchor_date                   DATE NOT NULL,   -- the date initial mastery was reached; every review_day offset counts from here
  status                         VARCHAR(20) NOT NULL,
  created_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (student_id, lesson_id, concept_id, academic_year_id)
);

-- ---------------------------------------------------------------------
-- review_schedule_items
-- Note: review_session_id references review_sessions, which is defined
-- below in this same file (circular). The column is declared here
-- without an inline FK and the constraint is added via ALTER TABLE
-- once review_sessions exists, later in this file.
-- ---------------------------------------------------------------------
CREATE TABLE review_schedule_items (
  review_schedule_item_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  review_schedule_id         UUID NOT NULL REFERENCES review_schedules(review_schedule_id),
  review_day                   INTEGER NOT NULL,   -- 2, 4, 8, 16, 32, 64, 128... continuing at a fixed ~30-day step past 32, until year end
  scheduled_for                 DATE NOT NULL,      -- = review_schedules.anchor_date + day_offset(review_day); computed once, never chained from a prior item
  status                         VARCHAR(20) NOT NULL
                                    CHECK (status IN ('UPCOMING','DUE','OVERDUE','COMPLETED','EXPIRED')),
  completed_at                    TIMESTAMPTZ,
  review_session_id                UUID,          -- FK to review_sessions added below (forward reference within this file)
  created_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (review_schedule_id, review_day)
);

CREATE INDEX idx_review_schedule_items_scheduled_status ON review_schedule_items (scheduled_for, status);

COMMENT ON TABLE review_schedule_items IS
  'CRITICAL RULE: a wrong answer (evidence_events.evaluation = INCORRECT during a review) never modifies scheduled_for on this row; per doc 11''s remediation loop, a NOT_RETAINED outcome instead typically resets the concept toward an earlier review_day via a fresh review_schedule_items row, application-layer, not by mutating this one in place. review_day values are seeded from a small, near-static reference list (2, 4, 8, 16, 32, then a fixed ~30-day step) -- global, not per-school, since the cadence itself is a locked product decision, not a school-configurable parameter.';

-- ---------------------------------------------------------------------
-- review_sessions -- groups several review questions worked
-- back-to-back, purely for the end-of-review-summary / proportional
-- -recap display (doc 11) -- it has no bearing on lesson_id
-- sessionization, which remains fully un-sessioned.
-- ---------------------------------------------------------------------
CREATE TABLE review_sessions (
  review_session_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id                 UUID NOT NULL REFERENCES student_profiles(student_id),
  review_schedule_item_id      UUID NOT NULL REFERENCES review_schedule_items(review_schedule_item_id),
  started_at                    TIMESTAMPTZ NOT NULL,
  completed_at                   TIMESTAMPTZ,
  status                          VARCHAR(20) NOT NULL,
  stop_reason                      VARCHAR(50),
  created_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Close the circular reference: review_schedule_items.review_session_id -> review_sessions.
ALTER TABLE review_schedule_items
  ADD CONSTRAINT fk_review_schedule_items_review_session
  FOREIGN KEY (review_session_id) REFERENCES review_sessions(review_session_id);

CREATE INDEX idx_review_sessions_student ON review_sessions (student_id);

-- ---------------------------------------------------------------------
-- review_session_questions -- test-first flow: the question is asked
-- before any material is re-shown (doc 11); anchor reuse is
-- application-layer, constrained to never repeat in two consecutive
-- sessions.
-- ---------------------------------------------------------------------
CREATE TABLE review_session_questions (
  review_session_question_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  review_session_id             UUID NOT NULL REFERENCES review_sessions(review_session_id),
  question_id                     UUID NOT NULL REFERENCES questions(question_id),
  sequence_number                  INTEGER NOT NULL,
  is_anchor_reuse                   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at                         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (review_session_id, sequence_number)
);

COMMENT ON TABLE review_session_questions IS
  'What still legitimately varies per student is WHICH concepts get scheduled and how a NOT_RETAINED outcome reshapes the upcoming items -- doc 11''s adaptive-hypothesis-test loop still applies to content selection, never to the fixed interval timing. Confidence-calibration (the student''s self-rated confidence before seeing the correct answer, per doc 11) and wrong-answer remediation are captured as evidence_events rows with evidence_context = SPACED_REVIEW (§9), not as separate tables here.';
