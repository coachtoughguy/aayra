-- =====================================================================
-- 017_deferred_capabilities.sql
-- Deferred Appendix — Designed, Not in the First Migration
-- (Physical Data Model, appendix before §19)
--
-- Two capabilities were pulled out of their home sections (§5, §14) by
-- the V3 sign-off, not because they're wrong, but because neither has
-- a pilot-school validation point yet and neither blocks the rest of
-- the schema. Both are Deferred: the table shapes below are considered
-- stable and ready to migrate in a later release once there's a
-- concrete trigger (a caregiver-access request for the first; Conduct
-- returning to a Principal-facing screen for the second).
--
-- The user's request explicitly asked to implement "all these tables"
-- with every key and unique condition, so — unlike the source
-- document's own phased rollout plan — this migration creates both
-- capabilities now, clearly labeled, rather than leaving them
-- unmigrated. Nothing elsewhere in this schema depends on either
-- table existing (student_guardians alone is sufficient for the
-- equal-access guardian model, and parent_attention_cases /
-- principal_followups, §13, carry the full escalation and follow-up
-- flow without a conduct_records FK).
-- =====================================================================

-- ---------------------------------------------------------------------
-- A. Delegated Guardian Access (deferred from §5)
-- A second, narrower-scope, independently-revocable grant -- e.g. a
-- caregiver who should see progress and messages but isn't a full
-- guardian -- distinct from the equal-access student_guardians tier
-- system. Locked by doc 13; unchanged from v1.
-- ---------------------------------------------------------------------
CREATE TABLE delegated_guardian_access (
  delegate_access_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id               UUID NOT NULL REFERENCES student_profiles(student_id),
  delegate_user_id             UUID NOT NULL REFERENCES users(user_id),
  granted_by                      UUID NOT NULL REFERENCES student_guardians(student_guardian_id),  -- must be an ACTIVE guardian_link holder for this student
  scope                              JSONB NOT NULL,   -- e.g. {"view_progress": true, "view_messages": true, "edit_assignments": false}
  granted_at                            TIMESTAMPTZ NOT NULL,
  revoked_at                               TIMESTAMPTZ,
  created_at                                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_delegated_guardian_access_student ON delegated_guardian_access (student_id);
CREATE INDEX idx_delegated_guardian_access_delegate_user ON delegated_guardian_access (delegate_user_id);

COMMENT ON TABLE delegated_guardian_access IS
  'granted_by must reference a student_guardians row with status = ACTIVE -- enforced at the application layer (a full FK CHECK against a conditional row state isn''t portable in Postgres). scope is intentionally coarser than a permissions framework: a small, fixed set of named booleans (view_progress, view_messages, edit_assignments, ...), not an open-ended ACL -- matches doc 13''s description of this as a narrow, specific grant, not a general delegation mechanism. revoked_at follows the same never-delete convention as student_guardians.unlinked_at (§5, §1) -- history of who could see what, and when, is retained.';

-- ---------------------------------------------------------------------
-- B. Conduct (deferred from §14)
-- A real Class Teacher/Admin conduct-recording capability with
-- per-record guardian visibility. Locked by doc 14; currently not
-- linked from any Principal-facing table in this MVP schema
-- (Principal PR-07 removed Conduct from the Principal MVP screen as a
-- deliberate, non-blocking scope call -- see §20).
-- ---------------------------------------------------------------------
CREATE TABLE conduct_records (
  conduct_record_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id              UUID NOT NULL REFERENCES schools(school_id),
  student_id                UUID NOT NULL REFERENCES student_profiles(student_id),
  recorded_by                  UUID NOT NULL REFERENCES users(user_id),   -- a Class Teacher or Admin
  section_id                      UUID NOT NULL REFERENCES sections(section_id),
  category                           VARCHAR(20) NOT NULL
                                        CHECK (category IN ('POSITIVE','CONCERN')),
  description                           TEXT NOT NULL,
  occurred_at                              TIMESTAMPTZ NOT NULL,
  visibility                                 VARCHAR(20) NOT NULL
                                                CHECK (visibility IN ('GUARDIAN_VISIBLE','INTERNAL_ONLY')),
  created_at                                    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_conduct_records_student ON conduct_records (student_id);

CREATE TABLE conduct_record_guardian_views (
  view_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conduct_record_id      UUID NOT NULL REFERENCES conduct_records(conduct_record_id),
  guardian_id                UUID NOT NULL REFERENCES guardian_profiles(guardian_id),
  viewed_at                     TIMESTAMPTZ NOT NULL,
  created_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_conduct_record_guardian_views_record ON conduct_record_guardian_views (conduct_record_id);

COMMENT ON TABLE conduct_records IS
  'visibility = INTERNAL_ONLY rows must never be returned to any student_guardians or delegated_guardian_access holder -- enforced at the query layer, the same pattern used for gating parent_attention_cases before it reaches a guardian (§13). No FK from principal_followups to conduct_records in this MVP schema, since Conduct doesn''t currently surface on the Principal screen -- see §19 for the full reconciliation. Both table sets are unchanged from their pre-V3 designs -- Deferred is a migration-sequencing decision, not a design downgrade.';
