-- =====================================================================
-- 014_academic_year_csv_onboarding.sql
-- Domain: Academic Year Lifecycle & Bulk Onboarding (Physical Data
-- Model §16)
-- Tables: academic_year_rollovers, csv_import_batches,
--         csv_import_row_results
-- Doc 02's Sept 9 addendum locks a per-row (not whole-file) validation
-- UX for CSV bulk onboarding, which Admin's import screen renders as a
-- per-row error list, and rollover as an explicit event rather than an
-- implicit consequence of creating new academic_years rows.
-- =====================================================================

CREATE TABLE academic_year_rollovers (
  rollover_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id               UUID NOT NULL REFERENCES schools(school_id),
  from_academic_year_id       UUID NOT NULL REFERENCES academic_years(academic_year_id),
  to_academic_year_id            UUID NOT NULL REFERENCES academic_years(academic_year_id),
  initiated_by                      UUID NOT NULL REFERENCES users(user_id),
  status                              VARCHAR(20) NOT NULL
                                         CHECK (status IN ('IN_PROGRESS','COMPLETED')),
  completed_at                          TIMESTAMPTZ,
  created_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_academic_year_rollovers_school ON academic_year_rollovers (school_id);

COMMENT ON TABLE academic_year_rollovers IS
  'An academic_year_rollovers event does not delete or overwrite the prior year''s student_enrollments / teacher_section_subject_assignments rows; it closes them (status changes) and the new academic_year_id''s grades/class_groups/sections tree gets fresh rows, so every table in §3-§4 stays queryable historically per year.';

CREATE TABLE csv_import_batches (
  batch_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id         UUID NOT NULL REFERENCES schools(school_id),
  imported_by          UUID NOT NULL REFERENCES users(user_id),
  file_name               VARCHAR(255) NOT NULL,
  import_type                VARCHAR(20) NOT NULL
                                CHECK (import_type IN ('STUDENTS','TEACHERS','PARENTS')),
  status                       VARCHAR(20) NOT NULL
                                  CHECK (status IN ('VALIDATING','VALIDATED_WITH_ERRORS','COMMITTED','FAILED')),
  submitted_at                    TIMESTAMPTZ NOT NULL,
  created_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_csv_import_batches_school_status ON csv_import_batches (school_id, status);

CREATE TABLE csv_import_row_results (
  row_result_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id             UUID NOT NULL REFERENCES csv_import_batches(batch_id),
  row_number               INTEGER NOT NULL,
  raw_row_data                JSONB NOT NULL,
  validation_status              VARCHAR(20) NOT NULL
                                    CHECK (validation_status IN ('VALID','ERROR')),
  error_reason                      VARCHAR(100),   -- e.g. duplicate_admission_id | invalid_section | primary_teacher_conflict | invalid_class_teacher_conflict
  resulting_user_id                    UUID REFERENCES users(user_id),   -- populated once this row is committed
  created_at                              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_csv_import_row_results_batch ON csv_import_row_results (batch_id);

COMMENT ON TABLE csv_import_row_results IS
  'A csv_import_batches batch commits row-by-row: rows with validation_status = VALID are applied even when other rows in the same file error out -- matches the Admin wireframes'' per-row (not whole-file-transaction) validation UX exactly. Bulk validation reuses the same constraints already defined on teacher_section_subject_assignments and student_enrollments (§4) -- error_reason values map directly onto those unique-index violations, rather than duplicating the rules in application code alone.';
