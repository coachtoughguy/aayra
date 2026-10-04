-- =====================================================================
-- 006_assignments_student_work.sql
-- Domain: Assignments & Student Work (Physical Data Model §7)
-- Tables: assignment_source_policies, assignments, assignment_targets,
--         assignment_dependencies, student_assignments,
--         assignment_due_date_exceptions, assignment_submissions,
--         submission_attachments
-- One unified assignments table (source_type SCHOOL/PARENT/SELF)
-- covers school-assigned, parent-assigned, and self-directed work with
-- one consistent lifecycle, separating the content (lesson, §6) from
-- the act of assigning it (assignments).
-- =====================================================================

-- ---------------------------------------------------------------------
-- assignment_source_policies -- Architecture-Locked: platform
-- configuration, not school data. Exactly 3 rows, seeded at migration
-- time, never school-editable: SCHOOL (all TRUE), PARENT (all FALSE),
-- SELF (all FALSE). Replaces a single is_subordinate_to_school_content
-- boolean, which only ever encoded "source_type = PARENT", one flag
-- for one rule; this table lets each source_type's platform-wide
-- behavior be looked up and extended (e.g. a future SELF row that can
-- create mastery evidence) without touching the assignments table's
-- schema.
-- ---------------------------------------------------------------------
CREATE TABLE assignment_source_policies (
  source_type                VARCHAR(20) PRIMARY KEY
                                CHECK (source_type IN ('SCHOOL','PARENT','SELF')),
  recommendation_priority    SMALLINT NOT NULL,        -- lower = higher priority; School > Parent > Self
  can_gate_school_learning   BOOLEAN NOT NULL,          -- may this source_type's assignments drive a lesson_dependencies chain or a review_item (§10)?
  can_create_mastery_evidence BOOLEAN NOT NULL,         -- may work against this assignment write to student_concept_mastery (§9)?
  can_schedule_retention_reviews BOOLEAN NOT NULL,      -- may this assignment's evidence seed review_schedule_items (§10)?
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO assignment_source_policies
  (source_type, recommendation_priority, can_gate_school_learning, can_create_mastery_evidence, can_schedule_retention_reviews)
VALUES
  ('SCHOOL', 1, TRUE,  TRUE,  TRUE),
  ('PARENT', 2, FALSE, FALSE, FALSE),
  ('SELF',   3, FALSE, FALSE, FALSE);

-- ---------------------------------------------------------------------
-- assignments
-- ---------------------------------------------------------------------
CREATE TABLE assignments (
  assignment_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id             UUID NOT NULL REFERENCES schools(school_id),
  lesson_id             UUID NOT NULL REFERENCES lessons(lesson_id),
  created_by            UUID NOT NULL REFERENCES users(user_id),
  source_type           VARCHAR(20) NOT NULL REFERENCES assignment_source_policies(source_type),
  title                 VARCHAR(250) NOT NULL,
  instructions          TEXT,
  available_from        TIMESTAMPTZ,
  due_at                TIMESTAMPTZ,
  status                VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
                           CHECK (status IN ('DRAFT','SCHEDULED','PUBLISHED','ACTIVE','CLOSED','ARCHIVED')),
  allow_late_submission BOOLEAN NOT NULL DEFAULT TRUE,
  version               INTEGER NOT NULL DEFAULT 1,        -- optimistic concurrency (Architecture-Locked, V3)
  published_at          TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_assignments_lesson ON assignments (lesson_id);
CREATE INDEX idx_assignments_source_type ON assignments (source_type);

COMMENT ON TABLE assignments IS
  'Intentionally does NOT itself carry mastery or review linkage -- a source_type = PARENT assignment must never be capable of gating a lesson_dependencies chain or a review_item (§9/§10); enforced by looking up assignment_source_policies.can_gate_school_learning / can_create_mastery_evidence for that source_type plus application-layer checks that never let a Parent assignment write to student_concept_mastery. assignment_source_policies is platform configuration (Architecture-Locked), never a school-scoped table -- no school_id column, no per-school override in the first migration.';

-- ---------------------------------------------------------------------
-- assignment_targets -- a Parent assignment always targets exactly one
-- STUDENT; a Teacher assignment usually targets a SECTION.
-- ---------------------------------------------------------------------
CREATE TABLE assignment_targets (
  assignment_target_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  assignment_id          UUID NOT NULL REFERENCES assignments(assignment_id),
  target_type            VARCHAR(20) NOT NULL
                            CHECK (target_type IN ('SECTION','STUDENT')),
  section_id             UUID REFERENCES sections(section_id),
  student_id             UUID REFERENCES student_profiles(student_id),
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    (target_type = 'SECTION' AND section_id IS NOT NULL AND student_id IS NULL)
    OR
    (target_type = 'STUDENT' AND student_id IS NOT NULL AND section_id IS NULL)
  )
);

CREATE INDEX idx_assignment_targets_assignment ON assignment_targets (assignment_id);

-- ---------------------------------------------------------------------
-- assignment_dependencies -- strict prerequisite list, no
-- sequential/flexible dependency mode.
-- ---------------------------------------------------------------------
CREATE TABLE assignment_dependencies (
  assignment_id              UUID NOT NULL REFERENCES assignments(assignment_id),
  prerequisite_assignment_id UUID NOT NULL REFERENCES assignments(assignment_id),
  PRIMARY KEY (assignment_id, prerequisite_assignment_id),
  CHECK (assignment_id <> prerequisite_assignment_id)
);

-- ---------------------------------------------------------------------
-- student_assignments -- materialized (one row per assignment x
-- targeted student) rather than derived live from assignment_targets
-- at read time, because Home/Calendar/Notifications all need to filter
-- and sort on status/effective_due_at cheaply and often. The canonical
-- per-student projection behind Home, Assignments, Calendar,
-- Notifications, Teacher & Parent Progress views.
-- ---------------------------------------------------------------------
CREATE TABLE student_assignments (
  student_assignment_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id               UUID NOT NULL REFERENCES schools(school_id),
  assignment_id            UUID NOT NULL REFERENCES assignments(assignment_id),
  student_id               UUID NOT NULL REFERENCES student_profiles(student_id),
  status                   VARCHAR(20) NOT NULL DEFAULT 'NOT_STARTED'
                              CHECK (status IN ('NOT_STARTED','IN_PROGRESS','SUBMITTED','SUBMITTED_LATE','COMPLETED','OVERDUE')),
  effective_due_at         TIMESTAMPTZ,     -- reflects the current deadline, including any due-date exception below
  started_at               TIMESTAMPTZ,
  submitted_at             TIMESTAMPTZ,
  completed_at             TIMESTAMPTZ,
  is_late                  BOOLEAN NOT NULL DEFAULT FALSE,
  version                  INTEGER NOT NULL DEFAULT 1,   -- optimistic concurrency (Architecture-Locked, V3)
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (assignment_id, student_id)
);

CREATE INDEX idx_student_assignments_student_status_due ON student_assignments (student_id, status, effective_due_at);
CREATE INDEX idx_student_assignments_assignment_status ON student_assignments (assignment_id, status);

-- ---------------------------------------------------------------------
-- assignment_due_date_exceptions -- backs Teacher TR-16 (Due-Date
-- Exception); student_assignments.effective_due_at is recomputed
-- whenever a row lands here.
-- ---------------------------------------------------------------------
CREATE TABLE assignment_due_date_exceptions (
  exception_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  assignment_id    UUID NOT NULL REFERENCES assignments(assignment_id),
  student_id       UUID NOT NULL REFERENCES student_profiles(student_id),
  original_due_at  TIMESTAMPTZ NOT NULL,
  new_due_at       TIMESTAMPTZ NOT NULL,
  reason           TEXT,
  created_by       UUID NOT NULL REFERENCES users(user_id),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (new_due_at > original_due_at)
);

CREATE INDEX idx_assignment_due_date_exceptions_assignment_student ON assignment_due_date_exceptions (assignment_id, student_id);

-- ---------------------------------------------------------------------
-- assignment_submissions
-- ---------------------------------------------------------------------
CREATE TABLE assignment_submissions (
  submission_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_assignment_id    UUID NOT NULL REFERENCES student_assignments(student_assignment_id),
  attempt_number           INTEGER NOT NULL,
  submitted_at             TIMESTAMPTZ NOT NULL,
  status                   VARCHAR(20) NOT NULL,
  student_note             TEXT,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (student_assignment_id, attempt_number)
);

-- ---------------------------------------------------------------------
-- submission_attachments
-- ---------------------------------------------------------------------
CREATE TABLE submission_attachments (
  attachment_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  submission_id      UUID NOT NULL REFERENCES assignment_submissions(submission_id) ON DELETE CASCADE,
  object_key         TEXT NOT NULL,
  mime_type          VARCHAR(150),
  file_name          VARCHAR(255),
  file_size_bytes    BIGINT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_submission_attachments_submission ON submission_attachments (submission_id);

COMMENT ON TABLE submission_attachments IS
  'One of the few tables that legitimately cascades (§19.3): a submission_attachments row is a truly dependent technical object of assignment_submissions, with no independent retention meaning.';
