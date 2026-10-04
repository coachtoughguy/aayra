-- =====================================================================
-- 005_content_lessons_ai_intelligence.sql
-- Domain: Content, Lessons & AI Content Intelligence (Physical Data
-- Model §6.1-6.3)
-- No AI sessionization: a lesson is one continuous content item plus a
-- focus timer, never Session 1/2/3 with fixed durations (Sept 16
-- decision addendum).
-- Tables: lessons, lesson_dependencies, content_items, lesson_content,
--         content_ownership, content_processing_jobs,
--         content_evidence_chunks, concepts, lesson_concepts,
--         concept_evidence_sources, concept_relationships,
--         assessment_blueprints, assessment_blueprint_concepts
-- =====================================================================

-- ---------------------------------------------------------------------
-- 6.1 Lessons & Sources
-- ---------------------------------------------------------------------
CREATE TABLE lessons (
  lesson_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id                 UUID NOT NULL REFERENCES schools(school_id),
  academic_year_id          UUID NOT NULL REFERENCES academic_years(academic_year_id),
  subject_id                UUID NOT NULL REFERENCES subjects(subject_id),
  created_by                UUID NOT NULL REFERENCES users(user_id),
  title                     VARCHAR(250) NOT NULL,
  description               TEXT,
  instructions              TEXT,
  requires_teacher_approval BOOLEAN NOT NULL DEFAULT TRUE,   -- AI-generated content still needs teacher sign-off, Sept 16 confirmed
  approved_by               UUID REFERENCES users(user_id),
  approved_at               TIMESTAMPTZ,
  status                    VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
                              CHECK (status IN ('DRAFT','PROCESSING','READY','SCHEDULED','PUBLISHED','ACTIVE','COMPLETED','ARCHIVED')),
  published_at              TIMESTAMPTZ,
  archived_at               TIMESTAMPTZ,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_lessons_subject ON lessons (subject_id);

COMMENT ON TABLE lessons IS
  'Deliberately NO session_number, target_duration, or session_count column: omitted by design, not just unused. Deliberately NO depends_on_lesson_id column: prerequisites are modeled via lesson_dependencies below (Architecture-Locked, V3).';

-- ---------------------------------------------------------------------
-- lesson_dependencies — forms a DAG (cycle-checked at the application
-- layer), walked to compute a student's next-available lesson (Student
-- Home's Locked-by-Dependency state). A join table, not a single
-- self-FK, so a lesson can carry more than one prerequisite.
-- ---------------------------------------------------------------------
CREATE TABLE lesson_dependencies (
  lesson_dependency_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lesson_id             UUID NOT NULL REFERENCES lessons(lesson_id),          -- the dependent lesson
  depends_on_lesson_id  UUID NOT NULL REFERENCES lessons(lesson_id),          -- the prerequisite lesson
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (lesson_id, depends_on_lesson_id),
  CHECK (lesson_id <> depends_on_lesson_id)
);

-- ---------------------------------------------------------------------
-- content_items — a reusable unit of raw source material (was
-- lesson_sources); no longer owned by exactly one lesson.
-- ---------------------------------------------------------------------
CREATE TABLE content_items (
  content_item_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id         UUID REFERENCES schools(school_id),    -- null = platform-shared content item, reusable across schools
  content_type      VARCHAR(30) NOT NULL
                       CHECK (content_type IN ('YOUTUBE','WEB_LINK','PDF','DOCUMENT','VIDEO','AUDIO','IMAGE','PHOTO','OTHER')),
  title             VARCHAR(250),
  source_url        TEXT,
  object_key        TEXT,                                   -- pointer into object storage; the file itself is never in Postgres
  mime_type         VARCHAR(150),
  file_size_bytes   BIGINT,
  duration_seconds  INTEGER,
  page_count        INTEGER,
  language_code     VARCHAR(20),
  uploaded_by       UUID NOT NULL REFERENCES users(user_id),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- lesson_content — the join: which content items compose this lesson,
-- and in what order; the same content item can appear in more than one
-- lesson_content row, across lessons or (for a platform-shared
-- content_item) across schools.
-- ---------------------------------------------------------------------
CREATE TABLE lesson_content (
  lesson_content_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lesson_id          UUID NOT NULL REFERENCES lessons(lesson_id),
  content_item_id    UUID NOT NULL REFERENCES content_items(content_item_id),
  display_order      INTEGER NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (lesson_id, content_item_id)
);

CREATE INDEX idx_lesson_content_lesson ON lesson_content (lesson_id, display_order);

-- ---------------------------------------------------------------------
-- content_ownership — kept separate from content_items itself so
-- ownership and ranking never get conflated.
-- ---------------------------------------------------------------------
CREATE TABLE content_ownership (
  ownership_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content_item_id  UUID UNIQUE NOT NULL REFERENCES content_items(content_item_id),
  owner_type       VARCHAR(20) NOT NULL
                      CHECK (owner_type IN ('SCHOOL','TEACHER','PARENT','STUDENT')),
  owner_user_id    UUID REFERENCES users(user_id),
  owner_school_id  UUID REFERENCES schools(school_id),
  origin_type      VARCHAR(20) NOT NULL
                      CHECK (origin_type IN ('SCHOOL_ASSIGNED','PARENT_ASSIGNED','SELF_LEARNING')),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE content_ownership IS
  'origin_type drives the "FROM YOU" badge in Teacher/Parent wireframes and the School > Parent > Self recommendation priority (§7). Keyed on content_item_id, not on a lesson: ownership and the badge belong to the content item itself and show consistently on every lesson that includes it via lesson_content, even when that item is reused across lessons.';

-- ---------------------------------------------------------------------
-- content_processing_jobs — backs the Teacher upload flow's
-- Uploading -> Processing -> Ready / Processing Failed + Retry states
-- directly.
-- ---------------------------------------------------------------------
CREATE TABLE content_processing_jobs (
  job_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id         UUID NOT NULL REFERENCES schools(school_id),
  content_item_id   UUID NOT NULL REFERENCES content_items(content_item_id),
  job_type          VARCHAR(30) NOT NULL
                       CHECK (job_type IN ('EXTRACTION','TRANSCRIPTION','OCR','FRAME_EXTRACTION','CCR_GENERATION','CONCEPT_EXTRACTION','EMBEDDING','ASSESSMENT_PREPARATION')),
  status            VARCHAR(20) NOT NULL DEFAULT 'QUEUED'
                       CHECK (status IN ('QUEUED','PROCESSING','SUCCEEDED','FAILED','RETRYING','CANCELLED')),
  attempt_count     INTEGER NOT NULL DEFAULT 0,
  started_at        TIMESTAMPTZ,
  completed_at      TIMESTAMPTZ,
  error_code        VARCHAR(100),
  error_message     TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_content_processing_jobs_status ON content_processing_jobs (status, created_at);
CREATE INDEX idx_content_processing_jobs_content_item ON content_processing_jobs (content_item_id);

COMMENT ON TABLE content_processing_jobs IS
  'A lessons.status = PROCESSING_FAILED-equivalent state is actually tracked per-job here, not on lessons itself, since one lesson''s content items can be at different processing stages simultaneously.';

-- ---------------------------------------------------------------------
-- 6.2 Concepts, Evidence Chunks & Relationships
-- ---------------------------------------------------------------------
CREATE TABLE content_evidence_chunks (
  chunk_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id          UUID NOT NULL REFERENCES schools(school_id),
  content_item_id    UUID NOT NULL REFERENCES content_items(content_item_id),
  chunk_type         VARCHAR(30) NOT NULL
                        CHECK (chunk_type IN ('TRANSCRIPT','OCR','DOCUMENT_TEXT','CODE','VISUAL_DESCRIPTION','TABLE')),
  content_text       TEXT NOT NULL,
  start_seconds      NUMERIC,
  end_seconds        NUMERIC,
  page_number        INTEGER,
  location_metadata  JSONB,
  confidence         NUMERIC(5,4),
  extraction_method  VARCHAR(50),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_content_evidence_chunks_content_item ON content_evidence_chunks (content_item_id);

COMMENT ON TABLE content_evidence_chunks IS
  'Provenance: what a mastery_check question or an AI Coach answer is actually grounded in.';

CREATE TABLE concepts (
  concept_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id        UUID REFERENCES schools(school_id),   -- null = a platform-shared concept, reusable across schools
  subject_id       UUID NOT NULL REFERENCES subjects(subject_id),
  name             VARCHAR(250) NOT NULL,
  normalized_name  VARCHAR(250) NOT NULL,                 -- lower/deduped form, for matching AI-extracted concepts to existing ones
  description      TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_concepts_subject ON concepts (subject_id);
CREATE INDEX idx_concepts_normalized_name ON concepts (normalized_name);

COMMENT ON COLUMN concepts.school_id IS
  'Nullable-for-shared is deliberate: it lets a platform-authored Grade 7 Math concept set be reused across schools while still allowing a school to define its own local concepts, without duplicating rows per school.';

CREATE TABLE lesson_concepts (
  lesson_concept_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lesson_id             UUID NOT NULL REFERENCES lessons(lesson_id),
  concept_id            UUID NOT NULL REFERENCES concepts(concept_id),
  importance            VARCHAR(20) NOT NULL
                           CHECK (importance IN ('CORE','SUPPORTING','STRETCH','PREREQUISITE')),
  is_explicitly_taught  BOOLEAN NOT NULL,
  confidence            NUMERIC(5,4),
  display_order         INTEGER,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (lesson_id, concept_id)
);

COMMENT ON TABLE lesson_concepts IS
  'The concept-coverage map (doc 11) that drives what an end-of-lesson assessment is allowed to test.';

CREATE TABLE concept_evidence_sources (
  concept_id        UUID NOT NULL REFERENCES concepts(concept_id),
  chunk_id          UUID NOT NULL REFERENCES content_evidence_chunks(chunk_id),
  relevance_score   NUMERIC(5,4),
  PRIMARY KEY (concept_id, chunk_id)
);

CREATE TABLE concept_relationships (
  relationship_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_concept_id     UUID NOT NULL REFERENCES concepts(concept_id),
  to_concept_id       UUID NOT NULL REFERENCES concepts(concept_id),
  relationship_type   VARCHAR(30) NOT NULL
                         CHECK (relationship_type IN ('PREREQUISITE_OF','RELATED_TO','PART_OF','BUILDS_ON','CONTRASTS_WITH')),
  confidence          NUMERIC(5,4),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_concept_relationships_from ON concept_relationships (from_concept_id);
CREATE INDEX idx_concept_relationships_to ON concept_relationships (to_concept_id);

COMMENT ON TABLE concept_relationships IS
  'The concept graph, represented relationally -- no graph database needed for MVP scale.';

-- ---------------------------------------------------------------------
-- 6.3 Internal Assessment Blueprint (AI-only, never teacher-facing)
-- ---------------------------------------------------------------------
CREATE TABLE assessment_blueprints (
  blueprint_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lesson_id           UUID NOT NULL REFERENCES lessons(lesson_id),
  generation_run_id   UUID NOT NULL,   -- provenance: which AI run produced this version (§17: ai_generation_runs).
                                        -- FK added by 015_analytics_audit_platform.sql once ai_generation_runs exists (forward reference).
  version             INTEGER NOT NULL,
  status              VARCHAR(20) NOT NULL,
  generated_at        TIMESTAMPTZ NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (lesson_id, version)
);

CREATE TABLE assessment_blueprint_concepts (
  blueprint_concept_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  blueprint_id          UUID NOT NULL REFERENCES assessment_blueprints(blueprint_id),
  concept_id            UUID NOT NULL REFERENCES concepts(concept_id),
  required_evidence     JSONB NOT NULL,     -- e.g. {"understanding": true, "reasoning": true, "application": true}
  priority              SMALLINT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE assessment_blueprints IS
  'Strictly internal: a teacher approves the lesson (lessons.approved_by), never the blueprint directly -- this preserves the "teacher approves content, not internal AI machinery" boundary from doc 03.';
