-- =====================================================================
-- 011_learning_dna.sql
-- Domain: Personalization / Learning DNA (Physical Data Model §12)
-- Tables: learning_observations, learning_hypotheses,
--         personalization_interventions
-- observe -> hypothesize -> personalize -> measure -> learn loop.
-- Never store a fixed personality label like "visual learner" -- store
-- dated, confidence-scored, re-testable observations instead. This
-- table set is this document's own concrete derivation from doc 04's
-- canonical content list (not yet a locked schema) and should be
-- treated as a first draft pending product sign-off -- unlike §2-§11,
-- which are direct derivations of already-LOCKED decisions and shipped
-- UI behavior.
-- =====================================================================

CREATE TABLE learning_observations (
  observation_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id             UUID NOT NULL REFERENCES student_profiles(student_id),
  observation_type         VARCHAR(50) NOT NULL,
    -- e.g. responds_better_to_examples | requires_more_support_on_transfer | preferred_time_of_day |
    -- hint_reliance_rate | avg_latency_to_answer | focus_cycle_completion_pattern | frequent_confusion_type
  context_type              VARCHAR(30),    -- e.g. LESSON, CONCEPT, SUBJECT -- what this observation is scoped to
  context_id                  UUID,          -- polymorphic pointer matching context_type
  observed_value                JSONB NOT NULL,
  confidence                     NUMERIC(5,4),
  observed_at                     TIMESTAMPTZ NOT NULL,
  expires_at                       TIMESTAMPTZ,   -- an observation can be time-bounded; stale signals shouldn't drive personalization indefinitely
  created_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_learning_observations_student_type ON learning_observations (student_id, observation_type, observed_at DESC);

COMMENT ON TABLE learning_observations IS
  'Populated from evidence_events, focus_cycles, and assessment_responses -- it is itself a derived/cache layer over those append-only sources, computed periodically, never a place raw events are written directly.';

CREATE TABLE learning_hypotheses (
  hypothesis_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id              UUID NOT NULL REFERENCES student_profiles(student_id),
  hypothesis_type           VARCHAR(50) NOT NULL,
  hypothesis_payload          JSONB NOT NULL,
  confidence                    NUMERIC(5,4),
  status                          VARCHAR(20) NOT NULL
                                     CHECK (status IN ('TESTING','SUPPORTED','WEAKENED','RETIRED')),
  created_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_learning_hypotheses_student_status ON learning_hypotheses (student_id, status);

CREATE TABLE personalization_interventions (
  intervention_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id               UUID NOT NULL REFERENCES student_profiles(student_id),
  hypothesis_id               UUID REFERENCES learning_hypotheses(hypothesis_id),
  intervention_type             VARCHAR(50) NOT NULL,
  before_state                    JSONB,
  intervention_payload              JSONB NOT NULL,
  started_at                         TIMESTAMPTZ NOT NULL,
  ended_at                            TIMESTAMPTZ,
  outcome                              JSONB,   -- free-form detail alongside the structured fields below
  metric_type                           VARCHAR(50),   -- what was measured, e.g. concept_mastery_rate, hint_reliance_rate, review_retention_rate
  baseline_metric                        NUMERIC(10,4),  -- the metric's value at started_at
  followup_metric                         NUMERIC(10,4),  -- the metric's value measured after the intervention ran
  measured_at                              TIMESTAMPTZ,    -- when followup_metric was captured
  status                                    VARCHAR(20) NOT NULL,
  created_at                                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_personalization_interventions_student ON personalization_interventions (student_id, started_at DESC);

COMMENT ON TABLE personalization_interventions IS
  'baseline_metric / followup_metric / metric_type / measured_at (V3 sign-off addition) give "did this intervention actually work?" a structured, queryable answer alongside the free-form outcome JSONB -- e.g. comparing hint_reliance_rate before and after, rather than leaving that comparison buried in unstructured JSON. The AI Coach overlay and any cross-lesson recommendation read from the current learning_hypotheses/learning_observations rows for grounding; they never recompute a full history live on each request.';
