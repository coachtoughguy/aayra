-- =====================================================================
-- 008_assessment_evidence_mastery.sql
-- Domain: Assessment, Evidence & Mastery (Physical Data Model §9)
-- Tables: questions, student_question_exposures, assessment_attempts,
--         assessment_responses, response_evaluations, evidence_events,
--         student_concept_mastery, mastery_state_history
-- Three-layer split: evidence_events (append-only, the historical
-- ledger), student_concept_mastery (current, derived state), and
-- mastery_state_history (every transition between the two, with the
-- specific evidence event that triggered it). One further split:
-- assessment_responses (what the student actually submitted, a fact)
-- and response_evaluations (the judgment made about that submission,
-- which can be superseded by a teacher's override in §11 without
-- rewriting the fact row).
-- =====================================================================

-- ---------------------------------------------------------------------
-- questions
-- ---------------------------------------------------------------------
CREATE TABLE questions (
  question_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lesson_id             UUID NOT NULL REFERENCES lessons(lesson_id),
  concept_id             UUID NOT NULL REFERENCES concepts(concept_id),
  question_type          VARCHAR(30) NOT NULL,    -- multiple_choice | open_ended | diagram_labeling | ...
  cognitive_type          VARCHAR(30) NOT NULL
                             CHECK (cognitive_type IN ('RECALL','UNDERSTANDING','REASONING','APPLICATION','TRANSFER')),
  prompt_text             TEXT NOT NULL,
  difficulty              VARCHAR(20),
  is_anchor               BOOLEAN NOT NULL DEFAULT FALSE,   -- a fixed reference question reused sparingly, never in consecutive review sessions
  semantic_fingerprint    TEXT,      -- normalized hash of the question's core ask; catches a regenerated paraphrase exact-id matching misses
  scenario_signature      TEXT,      -- normalized hash of the scenario/context wrapper, independent of the underlying concept ask
  source_provenance       JSONB,     -- which content_evidence_chunks this question was grounded in
  generated_by_model      VARCHAR(100),
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_questions_concept ON questions (concept_id);
CREATE INDEX idx_questions_lesson ON questions (lesson_id);
CREATE INDEX idx_questions_semantic_fingerprint ON questions (semantic_fingerprint);

-- ---------------------------------------------------------------------
-- student_question_exposures -- anti-repetition: before
-- selecting/generating a question, the exposure history for
-- (student, concept) is checked against question_id AND
-- semantic_fingerprint/scenario_signature, and recent/near-duplicate
-- items excluded.
-- ---------------------------------------------------------------------
CREATE TABLE student_question_exposures (
  exposure_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id       UUID NOT NULL REFERENCES student_profiles(student_id),
  question_id       UUID NOT NULL REFERENCES questions(question_id),
  concept_id         UUID NOT NULL REFERENCES concepts(concept_id),
  context            VARCHAR(20) NOT NULL
                        CHECK (context IN ('MASTERY_CHECK','REVIEW')),
  asked_at            TIMESTAMPTZ NOT NULL,
  evaluation          VARCHAR(20),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_student_question_exposures_student_concept_asked
  ON student_question_exposures (student_id, concept_id, asked_at DESC);

-- ---------------------------------------------------------------------
-- assessment_attempts -- never a fixed question-count stop reason like
-- "QUESTION_7_COMPLETE" -- mastery isn't gated on a fixed count
-- (Sept 16 confirmation).
-- ---------------------------------------------------------------------
CREATE TABLE assessment_attempts (
  attempt_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id                UUID NOT NULL REFERENCES student_profiles(student_id),
  lesson_id                  UUID NOT NULL REFERENCES lessons(lesson_id),
  student_assignment_id      UUID REFERENCES student_assignments(student_assignment_id),
  blueprint_id                UUID NOT NULL REFERENCES assessment_blueprints(blueprint_id),
  status                      VARCHAR(20) NOT NULL,
  started_at                  TIMESTAMPTZ NOT NULL,
  completed_at                TIMESTAMPTZ,
  stop_reason                 VARCHAR(50)
                                 CHECK (stop_reason IN ('SUFFICIENT_EVIDENCE','NEEDS_REVIEW','ABANDONED')),
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_assessment_attempts_student_lesson ON assessment_attempts (student_id, lesson_id);

-- ---------------------------------------------------------------------
-- assessment_responses -- FACT: what the student submitted, verbatim,
-- never re-judged in place.
-- ---------------------------------------------------------------------
CREATE TABLE assessment_responses (
  response_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  assessment_attempt_id  UUID NOT NULL REFERENCES assessment_attempts(attempt_id),
  question_id             UUID NOT NULL REFERENCES questions(question_id),
  response_sequence       INTEGER NOT NULL,
  response_payload        JSONB NOT NULL,     -- text answer, or an object-storage ref for diagram-labeling/photo evidence
  answered_at              TIMESTAMPTZ NOT NULL,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_assessment_responses_attempt ON assessment_responses (assessment_attempt_id);

-- ---------------------------------------------------------------------
-- response_evaluations -- INTERPRETATION: the judgment made about that
-- response. A response can be evaluated more than once (AI first, a
-- teacher's evaluation_adjustment later, §11); the prior row is kept
-- with is_current = FALSE rather than overwritten, so the AI's
-- original judgment is never lost.
-- ---------------------------------------------------------------------
CREATE TABLE response_evaluations (
  response_evaluation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  response_id               UUID NOT NULL REFERENCES assessment_responses(response_id),
  evaluated_by               VARCHAR(20) NOT NULL
                                CHECK (evaluated_by IN ('AI','TEACHER')),
  evaluation                 VARCHAR(20) NOT NULL
                                CHECK (evaluation IN ('CORRECT','PARTIAL','INCORRECT')),
  evaluator_confidence        NUMERIC(5,4),    -- low-confidence rows route into teacher_review_items, §11
  help_level                  VARCHAR(20)
                                 CHECK (help_level IN ('NONE','HINT','EXPLANATION')),   -- the hint -> explanation -> retry escalation path built into the Student lesson wireframes
  is_current                  BOOLEAN NOT NULL DEFAULT TRUE,
  evaluated_at                 TIMESTAMPTZ NOT NULL,
  created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one current evaluation per response.
CREATE UNIQUE INDEX idx_response_evaluations_one_current
  ON response_evaluations (response_id) WHERE is_current;

CREATE INDEX idx_response_evaluations_response ON response_evaluations (response_id);

-- ---------------------------------------------------------------------
-- evidence_events -- APPEND-ONLY. Never UPDATE. Never DELETE as part
-- of normal learning logic. The single source of truth every
-- downstream table below derives from.
-- ---------------------------------------------------------------------
CREATE TABLE evidence_events (
  evidence_event_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id                 UUID NOT NULL REFERENCES schools(school_id),
  student_id                 UUID NOT NULL REFERENCES student_profiles(student_id),
  lesson_id                   UUID NOT NULL REFERENCES lessons(lesson_id),
  assignment_id                UUID REFERENCES assignments(assignment_id),
  concept_id                    UUID NOT NULL REFERENCES concepts(concept_id),
  question_id                    UUID REFERENCES questions(question_id),
  response_evaluation_id          UUID REFERENCES response_evaluations(response_evaluation_id),  -- provenance; NULL when this evidence has no discrete student response (e.g. a teacher-entered evaluation)
  evidence_context                 VARCHAR(30) NOT NULL
                                      CHECK (evidence_context IN ('INITIAL_MASTERY','SPACED_REVIEW','TEACHER_EVALUATION')),
  evidence_type                     VARCHAR(30) NOT NULL
                                      CHECK (evidence_type IN ('UNDERSTANDING','REASONING','APPLICATION','TRANSFER','INDEPENDENCE','RETENTION')),
  evaluation                        VARCHAR(20) NOT NULL
                                      CHECK (evaluation IN ('CORRECT','PARTIAL','INCORRECT')),   -- snapshotted at evidence-recording time from response_evaluations.evaluation; the ledger row's meaning never silently drifts if a later teacher override changes which response_evaluations row is_current
  help_level                        VARCHAR(20),         -- snapshotted the same way
  evidence_strength                 NUMERIC(5,4),
  source_provenance                 JSONB,
  review_day                        INTEGER,             -- populated when evidence_context = 'SPACED_REVIEW', matches review_schedule_items.review_day
  occurred_at                       TIMESTAMPTZ NOT NULL,
  created_at                        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_evidence_events_student_concept_occurred ON evidence_events (student_id, concept_id, occurred_at DESC);
CREATE INDEX idx_evidence_events_lesson_concept_occurred ON evidence_events (lesson_id, concept_id, occurred_at DESC);

COMMENT ON TABLE evidence_events IS
  'The historical evidence ledger -- this table is the single source of truth every downstream table below derives from. Never written to directly by any UI action -- only in response to a submission or a teacher review action; a trigger or service-layer job then appends to mastery_state_history and upserts student_concept_mastery.';

-- ---------------------------------------------------------------------
-- student_concept_mastery -- derived/cache table, fully recomputable
-- from evidence_events; recomputed whenever a teacher confirms or
-- adjusts an evaluation (§11).
-- ---------------------------------------------------------------------
CREATE TABLE student_concept_mastery (
  mastery_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id            UUID NOT NULL REFERENCES student_profiles(student_id),
  concept_id             UUID NOT NULL REFERENCES concepts(concept_id),
  lesson_id               UUID NOT NULL REFERENCES lessons(lesson_id),
  mastery_status           VARCHAR(20) NOT NULL
                              CHECK (mastery_status IN ('NEEDS_REVIEW','DEVELOPING','MASTERED')),
  internal_confidence       NUMERIC(5,4),   -- never surfaced to any role directly; drives routing/scheduling only
  last_evidence_at           TIMESTAMPTZ,
  last_review_at              TIMESTAMPTZ,
  version                     INTEGER NOT NULL DEFAULT 1,
  created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (student_id, concept_id, lesson_id)
);

CREATE INDEX idx_student_concept_mastery_student_status ON student_concept_mastery (student_id, mastery_status);

-- ---------------------------------------------------------------------
-- mastery_state_history
-- ---------------------------------------------------------------------
CREATE TABLE mastery_state_history (
  history_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id                 UUID NOT NULL REFERENCES student_profiles(student_id),
  concept_id                  UUID NOT NULL REFERENCES concepts(concept_id),
  lesson_id                    UUID NOT NULL REFERENCES lessons(lesson_id),
  from_status                   VARCHAR(20),
  to_status                      VARCHAR(20) NOT NULL,
  internal_confidence            NUMERIC(5,4),
  trigger_evidence_event_id       UUID NOT NULL REFERENCES evidence_events(evidence_event_id),
  reason_code                     VARCHAR(50),
  changed_at                      TIMESTAMPTZ NOT NULL,
  created_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_mastery_state_history_student_concept ON mastery_state_history (student_id, concept_id, changed_at DESC);

COMMENT ON TABLE student_concept_mastery IS
  'Completion, Consistency, and Retention (doc 15''s other three dimensions, alongside Mastery) are each computed from their own source of truth, never duplicated here: Completion from student_lesson_progress.status (§8), Consistency from focus_cycles/review_attempts timestamps over a rolling window, Retention directly from evidence_events WHERE evidence_context = SPACED_REVIEW outcomes (§10).';
