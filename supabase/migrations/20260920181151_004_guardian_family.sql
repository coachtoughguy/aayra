-- =====================================================================
-- 004_guardian_family.sql
-- Domain: Guardian & Family Relationships (Physical Data Model §5,
-- Product-Locked part)
-- Tables: student_guardians
-- Note: delegated_guardian_access (a second, narrower-scope,
-- independently-revocable grant) is Deferred per the V3 sign-off --
-- designed, kept out of the first migration. See
-- 017_deferred_capabilities.sql.
-- =====================================================================

CREATE TABLE student_guardians (
  student_guardian_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id            UUID NOT NULL REFERENCES schools(school_id),
  student_id           UUID NOT NULL REFERENCES student_profiles(student_id),
  guardian_id          UUID NOT NULL REFERENCES guardian_profiles(guardian_id),
  relationship_type    VARCHAR(30) NOT NULL
                          CHECK (relationship_type IN ('MOTHER','FATHER','LEGAL_GUARDIAN','OTHER')),
  guardian_priority    VARCHAR(20) NOT NULL
                          CHECK (guardian_priority IN ('PRIMARY','SECONDARY','TERTIARY')),
  status               VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                          CHECK (status IN ('ACTIVE','UNLINKED')),
  linked_at            TIMESTAMPTZ NOT NULL,
  linked_by            UUID NOT NULL REFERENCES users(user_id),
  unlinked_at          TIMESTAMPTZ,
  unlinked_by          UUID REFERENCES users(user_id),
  unlink_reason        TEXT,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (student_id, guardian_id)
);

-- At most one ACTIVE PRIMARY guardian per student.
CREATE UNIQUE INDEX idx_student_guardians_one_active_primary
  ON student_guardians (student_id)
  WHERE guardian_priority = 'PRIMARY' AND status = 'ACTIVE';

CREATE INDEX idx_student_guardians_guardian ON student_guardians (guardian_id);

COMMENT ON TABLE student_guardians IS
  'Primary/Secondary/Tertiary is a contact-priority designation, NOT an access-level hierarchy: all linked guardians get equal, full access -- no access-level column here by design. A student can exist with no guardian yet ("Primary Guardian Pending" is a derived UI state, not a placeholder row). Rows are only ever UNLINKED, never deleted -- preserves historical message and progress-report attribution across a custody or guardianship change.';

COMMENT ON COLUMN student_guardians.student_id IS
  'Not a student user_id -- this is the correct FK target for every guardian relationship and permission check, since it must resolve for sub-13 students who never get a users row with login capability.';

COMMENT ON TABLE student_guardians IS
  'family_group (a convenience grouping for a guardian''s multi-child household view) is intentionally NOT modeled as its own table -- it is fully derivable from student_guardians grouped by guardian_id, so it is a query/read-model concern (§15), not a source-of-truth table.';
