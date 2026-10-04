-- =====================================================================
-- 003_people_enrollment_teaching.sql
-- Domain: People, Enrollment & Teaching Assignments (Physical Data
-- Model §4)
-- Tables: student_profiles, teacher_profiles, guardian_profiles,
--         student_enrollments, teacher_section_subject_assignments,
--         teacher_assignment_history
-- Role-profile pattern: student_profiles / teacher_profiles /
-- guardian_profiles each extend users 1:1, rather than one generic
-- person table, because each profile's columns genuinely differ.
-- =====================================================================

-- ---------------------------------------------------------------------
-- student_profiles
-- ---------------------------------------------------------------------
CREATE TABLE student_profiles (
  student_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  user_id             UUID UNIQUE REFERENCES users(user_id),   -- null for a permanently sub-13, no-login student
  admission_id        VARCHAR(100) NOT NULL,
  date_of_birth       DATE NOT NULL,
  preferred_language  VARCHAR(20),
  status              VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (school_id, admission_id)
);

-- ---------------------------------------------------------------------
-- teacher_profiles
-- ---------------------------------------------------------------------
CREATE TABLE teacher_profiles (
  teacher_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  user_id             UUID UNIQUE NOT NULL REFERENCES users(user_id),
  employee_number     VARCHAR(100),
  status              VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (school_id, employee_number)
);

-- ---------------------------------------------------------------------
-- guardian_profiles — deliberately NOT school-scoped: one guardian can
-- have children across schools.
-- ---------------------------------------------------------------------
CREATE TABLE guardian_profiles (
  guardian_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID UNIQUE NOT NULL REFERENCES users(user_id),
  preferred_language  VARCHAR(20),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- student_enrollments — the canonical per-student-per-year roster row
-- ---------------------------------------------------------------------
CREATE TABLE student_enrollments (
  enrollment_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  student_id          UUID NOT NULL REFERENCES student_profiles(student_id),
  academic_year_id    UUID NOT NULL REFERENCES academic_years(academic_year_id),
  grade_id            UUID NOT NULL REFERENCES grades(grade_id),
  section_id          UUID NOT NULL REFERENCES sections(section_id),  -- class_group (if any) resolved via sections.class_group_id, §3
  enrollment_date     DATE NOT NULL,
  exit_date           DATE,
  status              VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','TRANSFERRED','WITHDRAWN','COMPLETED')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (student_id, academic_year_id)
);

CREATE INDEX idx_student_enrollments_section ON student_enrollments (section_id);

COMMENT ON TABLE student_enrollments IS
  'One active enrollment per student per year (section-singularity lock, doc 02 v1.0 addendum). A mid-year section change is a NEW row plus the old row''s status -> TRANSFERRED, never an UPDATE of section_id in place.';

-- ---------------------------------------------------------------------
-- teacher_section_subject_assignments
-- ---------------------------------------------------------------------
CREATE TABLE teacher_section_subject_assignments (
  teacher_assignment_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id              UUID NOT NULL REFERENCES schools(school_id),
  academic_year_id       UUID NOT NULL REFERENCES academic_years(academic_year_id),
  teacher_id             UUID NOT NULL REFERENCES teacher_profiles(teacher_id),
  section_id             UUID NOT NULL REFERENCES sections(section_id),
  subject_id             UUID REFERENCES subjects(subject_id),   -- null when assignment_role = CLASS_TEACHER (pastoral, not subject-bound)
  assignment_role        VARCHAR(30) NOT NULL
                            CHECK (assignment_role IN ('CLASS_TEACHER','PRIMARY_SUBJECT_TEACHER','CO_TEACHER')),
  effective_from         DATE NOT NULL,
  effective_to           DATE,
  status                 VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                            CHECK (status IN ('ACTIVE','INACTIVE')),
  created_by             UUID REFERENCES users(user_id),
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one active CLASS_TEACHER per section.
CREATE UNIQUE INDEX idx_tssa_one_active_class_teacher
  ON teacher_section_subject_assignments (section_id)
  WHERE assignment_role = 'CLASS_TEACHER' AND status = 'ACTIVE';

-- Exactly one active PRIMARY_SUBJECT_TEACHER per section+subject.
CREATE UNIQUE INDEX idx_tssa_one_active_primary_subject_teacher
  ON teacher_section_subject_assignments (section_id, subject_id)
  WHERE assignment_role = 'PRIMARY_SUBJECT_TEACHER' AND status = 'ACTIVE';

-- CO_TEACHER rows are unconstrained in count.

CREATE INDEX idx_tssa_teacher ON teacher_section_subject_assignments (teacher_id);

COMMENT ON TABLE teacher_section_subject_assignments IS
  'Never DELETE or hard-overwrite a row on a handover: set effective_to / status = INACTIVE, insert the replacement, and log both in teacher_assignment_history -- this is what lets content and evidence authored under the outgoing teacher''s tenure keep their original created_by.';

-- ---------------------------------------------------------------------
-- teacher_assignment_history — explicit handover log (doc 20's
-- teacher-handover contract: a Primary handover must be traceable, not
-- just inferred from two overlapping date ranges)
-- ---------------------------------------------------------------------
CREATE TABLE teacher_assignment_history (
  history_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  teacher_assignment_id   UUID NOT NULL REFERENCES teacher_section_subject_assignments(teacher_assignment_id),
  event                   VARCHAR(20) NOT NULL
                            CHECK (event IN ('assigned','handed_over','deactivated')),
  actor_id                UUID NOT NULL REFERENCES users(user_id),
  occurred_at             TIMESTAMPTZ NOT NULL,
  note                    TEXT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_teacher_assignment_history_assignment ON teacher_assignment_history (teacher_assignment_id);

COMMENT ON TABLE teacher_assignment_history IS
  'Bulk CSV onboarding (§14) validates against this domain row-by-row before insert: a row that would violate the one-active-Primary/Class-Teacher constraints, reference a non-existent section_id, or duplicate an admission_id is rejected individually, not as a whole-file transaction failure.';
