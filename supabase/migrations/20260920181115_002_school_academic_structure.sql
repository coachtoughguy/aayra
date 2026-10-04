-- =====================================================================
-- 002_school_academic_structure.sql
-- Domain: School & Academic Structure (Physical Data Model §3)
-- Tables: academic_years, grades, class_groups, sections, subjects,
--         grade_subjects
-- Hierarchy: School -> AcademicYear -> Grade -> [Class Group] -> Section
-- Class Group is Pilot-Configurable (optional level between Grade and
-- Section); Grade and Section are Product-Locked.
-- =====================================================================

-- ---------------------------------------------------------------------
-- academic_years
-- ---------------------------------------------------------------------
CREATE TABLE academic_years (
  academic_year_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  name                VARCHAR(50) NOT NULL,          -- "2026-27"
  start_date          DATE NOT NULL,
  end_date            DATE NOT NULL,
  status              VARCHAR(20) NOT NULL
                        CHECK (status IN ('PLANNED','ACTIVE','CLOSED','ARCHIVED')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (school_id, name),
  CHECK (end_date > start_date)
);

-- Exactly one ACTIVE academic year per school.
CREATE UNIQUE INDEX idx_academic_years_one_active_per_school
  ON academic_years (school_id) WHERE status = 'ACTIVE';

COMMENT ON TABLE academic_years IS
  'Everything below this level (grades/class_groups/sections) cascades from academic_year_id, so a rollover creates a fresh tree for the new year rather than mutating existing rows.';

-- ---------------------------------------------------------------------
-- grades
-- ---------------------------------------------------------------------
CREATE TABLE grades (
  grade_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  academic_year_id    UUID NOT NULL REFERENCES academic_years(academic_year_id),
  name                VARCHAR(100) NOT NULL,         -- "Grade 7"
  grade_level         SMALLINT NOT NULL,              -- 1..12, for promotion/sequencing logic
  display_order       SMALLINT NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (academic_year_id, name)
);

-- ---------------------------------------------------------------------
-- class_groups — Pilot-Configurable optional level between Grade and
-- Section (e.g. a named home room within a grade). A school that
-- doesn't organize by named class groupings simply never populates
-- this table and leaves sections.class_group_id NULL.
-- ---------------------------------------------------------------------
CREATE TABLE class_groups (
  class_group_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  academic_year_id    UUID NOT NULL REFERENCES academic_years(academic_year_id),
  grade_id            UUID NOT NULL REFERENCES grades(grade_id),
  name                VARCHAR(100) NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (grade_id, name),
  UNIQUE (class_group_id, grade_id)               -- composite target for sections' guarded FK
);

-- ---------------------------------------------------------------------
-- sections — the actual roster unit
-- ---------------------------------------------------------------------
CREATE TABLE sections (
  section_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  grade_id            UUID NOT NULL REFERENCES grades(grade_id),   -- direct FK; always required
  class_group_id      UUID,                                        -- optional; Pilot-Configurable
  name                VARCHAR(50) NOT NULL,           -- "Section B"
  capacity            INTEGER,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (grade_id, class_group_id, name),
  FOREIGN KEY (class_group_id, grade_id) REFERENCES class_groups (class_group_id, grade_id)
    -- MATCH SIMPLE (Postgres default): satisfied automatically when class_group_id IS NULL,
    -- and otherwise guards against a section referencing a class group from the wrong grade.
);

CREATE INDEX idx_sections_grade ON sections (grade_id);

COMMENT ON COLUMN sections.class_group_id IS
  'A student''s one-active-section-per-year lock (doc 02 v1.0 Guardian Model addendum) is enforced on student_enrollments (§4), not here.';

-- ---------------------------------------------------------------------
-- subjects — school-configurable, unlike roles (§2)
-- ---------------------------------------------------------------------
CREATE TABLE subjects (
  subject_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  academic_year_id    UUID NOT NULL REFERENCES academic_years(academic_year_id),
  code                VARCHAR(50) NOT NULL,
  name                VARCHAR(150) NOT NULL,
  description         TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (school_id, academic_year_id, code)
);

-- ---------------------------------------------------------------------
-- grade_subjects
-- ---------------------------------------------------------------------
CREATE TABLE grade_subjects (
  grade_subject_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  grade_id            UUID NOT NULL REFERENCES grades(grade_id),
  subject_id          UUID NOT NULL REFERENCES subjects(subject_id),
  is_required         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (grade_id, subject_id)
);
