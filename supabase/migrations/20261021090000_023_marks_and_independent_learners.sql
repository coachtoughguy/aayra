-- 023_marks_and_independent_learners
-- PENDING: not yet applied to Supabase.
--
-- 1. Marks on submissions (doc 12 "Marks, mastery, and ranking policy"):
--    marks are optional per assignment, and either teacher-only or visible to the student.
--    Marks never feed mastery, and nothing ranks students. Late work keeps the mark the teacher gives
--    (doc 21: no automatic deduction).
-- 2. Independent adult self-learners (docs 02/05, Sept 15 locks; Oct 20 decision "personal space"):
--    each 18+ learner without a school gets a private INDEPENDENT space shaped like a school
--    (one year, one grade, one section), so every tenancy and privacy rule keeps working unchanged.
-- All changes are additive.

-- 1. Marks ---------------------------------------------------------------------------------------
ALTER TABLE assignments
  ADD COLUMN max_marks numeric(7,2),
  ADD COLUMN marks_visibility character varying(20) NOT NULL DEFAULT 'TEACHER_ONLY';

ALTER TABLE assignments
  ADD CONSTRAINT assignments_marks_visibility_check
    CHECK (marks_visibility IN ('TEACHER_ONLY', 'STUDENT_VISIBLE')),
  ADD CONSTRAINT assignments_max_marks_positive CHECK (max_marks IS NULL OR max_marks > 0),
  ADD CONSTRAINT assignments_marks_need_submission
    CHECK (max_marks IS NULL OR completion_mode IN ('SUBMISSION_REQUIRED', 'BOTH'));

COMMENT ON COLUMN assignments.max_marks IS
  'Optional. NULL = this assignment is not marked. Only for assignments that take a submission.';

ALTER TABLE assignment_submissions
  ADD COLUMN marks_awarded numeric(7,2),
  ADD COLUMN marked_by uuid REFERENCES users(user_id),
  ADD COLUMN marked_at timestamptz,
  ADD COLUMN marks_released_at timestamptz;

ALTER TABLE assignment_submissions
  ADD CONSTRAINT assignment_submissions_marks_non_negative CHECK (marks_awarded IS NULL OR marks_awarded >= 0),
  ADD CONSTRAINT assignment_submissions_marks_attributed
    CHECK ((marks_awarded IS NULL) = (marked_by IS NULL) AND (marks_awarded IS NULL) = (marked_at IS NULL)),
  ADD CONSTRAINT assignment_submissions_release_needs_marks
    CHECK (marks_released_at IS NULL OR marks_awarded IS NOT NULL);

COMMENT ON COLUMN assignment_submissions.marks_released_at IS
  'When the teacher released the mark. Students see it only if released AND the assignment is STUDENT_VISIBLE.';

-- 2. Independent learners -------------------------------------------------------------------------
ALTER TABLE schools
  ADD COLUMN school_kind character varying(20) NOT NULL DEFAULT 'SCHOOL',
  ADD COLUMN owner_user_id uuid REFERENCES users(user_id);

ALTER TABLE schools
  ADD CONSTRAINT schools_school_kind_check CHECK (school_kind IN ('SCHOOL', 'INDEPENDENT')),
  ADD CONSTRAINT schools_independent_has_owner CHECK ((school_kind = 'INDEPENDENT') = (owner_user_id IS NOT NULL));

CREATE UNIQUE INDEX schools_one_independent_space_per_user
  ON schools (owner_user_id) WHERE school_kind = 'INDEPENDENT';

COMMENT ON COLUMN schools.school_kind IS
  'SCHOOL = a real school. INDEPENDENT = one adult self-learner''s private space (no teachers, no guardians).';

-- Direct signup is 18+ only (doc 05 §18.5). The exact verification mechanism is still OPEN, so the
-- learner's own date-of-birth declaration is recorded as such.
ALTER TABLE users DROP CONSTRAINT users_age_gate_status_check;
ALTER TABLE users ADD CONSTRAINT users_age_gate_status_check
  CHECK (age_gate_status IN ('not_applicable', 'verified_13_plus', 'blocked_under_13', 'self_declared_18_plus'));
