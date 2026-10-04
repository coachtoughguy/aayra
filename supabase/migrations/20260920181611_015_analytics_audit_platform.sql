-- =====================================================================
-- 015_analytics_audit_platform.sql
-- Domain: Analytics, Audit & Platform Operations (Physical Data Model
-- §17)
-- Tables: class_insight_snapshots, audit_events, ai_invocations,
--         background_jobs, domain_event_outbox, ai_generation_runs,
--         ai_context_reference, idempotency_records
-- A general audit log, a cached read-model layer for dashboards, an
-- AI-invocation audit distinct from general audit (privacy-sensitive),
-- background job tracking, and a domain-event outbox for reliable
-- event delivery without starting with microservices.
-- =====================================================================

-- ---------------------------------------------------------------------
-- class_insight_snapshots — rebuildable cache in front of
-- Teacher/Principal dashboards, never computed live from millions of
-- evidence_events rows.
-- ---------------------------------------------------------------------
CREATE TABLE class_insight_snapshots (
  snapshot_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id                  UUID NOT NULL REFERENCES schools(school_id),
  section_id                    UUID NOT NULL REFERENCES sections(section_id),
  subject_id                       UUID NOT NULL REFERENCES subjects(subject_id),
  assignment_id                       UUID REFERENCES assignments(assignment_id),
  concept_id                             UUID REFERENCES concepts(concept_id),
  window_start                              DATE NOT NULL,
  window_end                                   DATE NOT NULL,
  eligible_student_count                          INTEGER NOT NULL,
  completed_student_count                            INTEGER NOT NULL,
  metric_payload                                        JSONB NOT NULL,
  calculation_version                                      VARCHAR(20) NOT NULL,   -- which computation logic version produced this row; a logic change gets a new value, not a silent recompute
  evidence_cutoff_at                                          TIMESTAMPTZ NOT NULL,  -- the latest evidence_events.occurred_at considered; makes "as of when" explicit and auditable
  generated_at                                                   TIMESTAMPTZ NOT NULL,
  created_at                                                       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                                        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_class_insight_snapshots_section_window ON class_insight_snapshots (section_id, window_start, window_end);

COMMENT ON TABLE class_insight_snapshots IS
  'Like student_concept_mastery (§9), a rebuildable cache: it can be dropped and recomputed entirely from evidence_events, student_assignments, and student_lesson_progress at any time, and nothing else has a hard dependency on it existing. calculation_version/evidence_cutoff_at mean a recompute never silently changes what an existing row means -- a logic change produces new rows at a new calculation_version, not a mutated old one. An assignment-level concept insight needs completed/eligible >= 75% before it''s eligible to roll up to class-level (exact threshold configurable, not yet locked).';

-- ---------------------------------------------------------------------
-- audit_events — general-purpose log for anything not already covered
-- by a domain-specific *_status_history table (the convention from
-- §1).
-- ---------------------------------------------------------------------
CREATE TABLE audit_events (
  audit_event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id             UUID REFERENCES schools(school_id),
  actor_user_id            UUID REFERENCES users(user_id),
  actor_role                  SMALLINT REFERENCES roles(role_id),
  action                         VARCHAR(100) NOT NULL,
    -- STUDENT_ENROLLED | GUARDIAN_LINKED | GUARDIAN_UNLINKED | TEACHER_MAPPING_CHANGED | ASSIGNMENT_PUBLISHED |
    -- DUE_DATE_CHANGED | TEACHER_REVIEW_COMPLETED | PARENT_ATTENTION_APPROVED | PRINCIPAL_FOLLOWUP_UPDATED | ...
  entity_type                       VARCHAR(50) NOT NULL,
  entity_id                            UUID NOT NULL,
  before_data                             JSONB,
  after_data                                 JSONB,
  request_id                                    UUID,
  occurred_at                                     TIMESTAMPTZ NOT NULL,
  created_at                                        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_events_school_occurred ON audit_events (school_id, occurred_at DESC);
CREATE INDEX idx_audit_events_entity ON audit_events (entity_type, entity_id);

COMMENT ON TABLE audit_events IS
  'Avoid storing sensitive content (message bodies, conduct descriptions) in before_data/after_data -- reference by id instead. ai_invocations is deliberately separate because it carries a stricter privacy bar (no raw prompt/response bodies) than general audit.';

-- ---------------------------------------------------------------------
-- ai_invocations — real-time per-request AI calls (a Coach answer, a
-- live evaluation); privacy-safe operational metadata only, never
-- blindly persists full student prompts/responses here.
-- ---------------------------------------------------------------------
CREATE TABLE ai_invocations (
  invocation_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id             UUID NOT NULL REFERENCES schools(school_id),
  user_id                  UUID REFERENCES users(user_id),
  student_id                  UUID REFERENCES student_profiles(student_id),
  purpose                        VARCHAR(50) NOT NULL,   -- e.g. ai_coach_answer, concept_extraction, evaluation
  entity_type                       VARCHAR(50),
  entity_id                            UUID,
  model_name                              VARCHAR(100) NOT NULL,
  prompt_version                             VARCHAR(50),
  input_token_count                             INTEGER,
  output_token_count                               INTEGER,
  latency_ms                                          INTEGER,
  status                                                 VARCHAR(20) NOT NULL,
  guardrail_result                                          JSONB,
  created_at                                                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ai_invocations_student ON ai_invocations (student_id);
CREATE INDEX idx_ai_invocations_school_created ON ai_invocations (school_id, created_at DESC);

-- ---------------------------------------------------------------------
-- background_jobs — orchestration bookkeeping; the actual queue can be
-- Redis/SQS/etc. -- this table is not the queue itself.
-- ---------------------------------------------------------------------
CREATE TABLE background_jobs (
  job_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id             UUID REFERENCES schools(school_id),
  job_type                 VARCHAR(50) NOT NULL,
  entity_type                  VARCHAR(50),
  entity_id                        UUID,
  status                              VARCHAR(20) NOT NULL,
  attempt_count                          INTEGER NOT NULL DEFAULT 0,
  max_attempts                              INTEGER NOT NULL,
  scheduled_at                                 TIMESTAMPTZ,
  started_at                                      TIMESTAMPTZ,
  completed_at                                       TIMESTAMPTZ,
  last_error                                            TEXT,
  created_at                                               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_background_jobs_status_scheduled ON background_jobs (status, scheduled_at);

-- ---------------------------------------------------------------------
-- domain_event_outbox — durable event-publication state; gives
-- reliable event-driven processing without starting with
-- microservices. Rows are written in the same transaction as the
-- domain change they describe, then published asynchronously and
-- marked published_at.
-- ---------------------------------------------------------------------
CREATE TABLE domain_event_outbox (
  event_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id             UUID REFERENCES schools(school_id),
  aggregate_type            VARCHAR(50) NOT NULL,
  aggregate_id                  UUID NOT NULL,
  event_type                       VARCHAR(100) NOT NULL,
    -- AssignmentPublished | LessonCompleted | MasteryUpdated | ReviewDue | ReviewCompleted |
    -- ParentAttentionApproved | TeacherFeedbackPublished | PrincipalFollowupCreated | ...
  payload                              JSONB NOT NULL,
  occurred_at                             TIMESTAMPTZ NOT NULL,
  published_at                               TIMESTAMPTZ,
  status                                        VARCHAR(20) NOT NULL,
  created_at                                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_domain_event_outbox_status ON domain_event_outbox (status, occurred_at);

-- ---------------------------------------------------------------------
-- ai_generation_runs — batch/pipeline provenance (a concept-extraction
-- or blueprint-generation job); distinct from ai_invocations (real-time
-- per-request calls) because they have different retention needs and
-- different callers.
-- ---------------------------------------------------------------------
CREATE TABLE ai_generation_runs (
  generation_run_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id              UUID REFERENCES schools(school_id),   -- null for a platform-shared generation (e.g. a shared concept set)
  run_type                  VARCHAR(40) NOT NULL,
    -- CONCEPT_EXTRACTION | CONCEPT_RELATIONSHIP_INFERENCE | ASSESSMENT_BLUEPRINT_GENERATION |
    -- EVIDENCE_CHUNKING | QUESTION_GENERATION
  entity_type                  VARCHAR(50) NOT NULL,   -- e.g. 'lesson', 'content_item', 'assessment_blueprint'
  entity_id                        UUID NOT NULL,
  model_name                          VARCHAR(100) NOT NULL,
  prompt_version                         VARCHAR(50),
  status                                    VARCHAR(20) NOT NULL
                                               CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')),
  input_summary                                JSONB,
  output_summary                                  JSONB,
  started_at                                         TIMESTAMPTZ,
  completed_at                                          TIMESTAMPTZ,
  created_at                                               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ai_generation_runs_entity ON ai_generation_runs (entity_type, entity_id);

COMMENT ON TABLE ai_generation_runs IS
  'Provenance for any AI-generated artifact in §6''s content-intelligence domain; assessment_blueprints.generation_run_id is the one dedicated FK column -- concept extraction and relationship inference are looked up by (entity_type, entity_id) rather than adding an FK column to every AI-touched table.';

-- Close the forward reference declared in 005_content_lessons_ai_intelligence.sql:
-- assessment_blueprints.generation_run_id -> ai_generation_runs.
ALTER TABLE assessment_blueprints
  ADD CONSTRAINT fk_assessment_blueprints_generation_run
  FOREIGN KEY (generation_run_id) REFERENCES ai_generation_runs(generation_run_id);

-- ---------------------------------------------------------------------
-- ai_context_reference — answers "what did the AI see when it said
-- this?" without persisting full prompts/responses in ai_invocations
-- itself.
-- ---------------------------------------------------------------------
CREATE TABLE ai_context_reference (
  context_reference_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ai_invocation_id          UUID NOT NULL REFERENCES ai_invocations(invocation_id),
  context_type                 VARCHAR(50) NOT NULL,   -- e.g. 'content_evidence_chunk', 'evidence_event', 'learning_observation'
  context_entity_id                UUID NOT NULL,
  relevance_note                      TEXT,
  created_at                             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ai_context_reference_invocation ON ai_context_reference (ai_invocation_id);

-- ---------------------------------------------------------------------
-- idempotency_records — deliberately generic platform infrastructure,
-- not domain data: no FK from any domain table points at it, and a
-- domain write only ever checks it before proceeding, the same
-- pattern regardless of which endpoint is calling.
-- ---------------------------------------------------------------------
CREATE TABLE idempotency_records (
  idempotency_key    VARCHAR(255) PRIMARY KEY,     -- client-supplied, generated once per user action/retry-safe request
  school_id              UUID REFERENCES schools(school_id),
  request_fingerprint        TEXT NOT NULL,           -- hash of the request payload, to detect a genuinely different request reusing the same key
  response_snapshot             JSONB,                -- the response returned the first time, replayed verbatim on a retry
  status                           VARCHAR(20) NOT NULL
                                      CHECK (status IN ('IN_PROGRESS','COMPLETED','FAILED')),
  created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at                             TIMESTAMPTZ NOT NULL    -- keys are not retained forever; a short TTL (e.g. 24h) covers client retry windows
);

CREATE INDEX idx_idempotency_records_expires_at ON idempotency_records (expires_at);

COMMENT ON TABLE idempotency_records IS
  'Guards every write endpoint a flaky mobile network might retry (assignment submission, evidence-event-producing calls) against double-processing; not domain data, deliberately generic infrastructure.';
