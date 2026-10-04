-- =====================================================================
-- 016_compliance_consent.sql
-- Domain: Compliance & Consent (Physical Data Model §18)
-- Tables: market_compliance_profiles, retention_policies,
--         consent_records, school_agreements, data_subject_requests
-- Aayra operates across markets with different regulatory regimes for
-- student data (doc 20's Global Compliance & Consent Architecture
-- addendum). Structure (one retention_policies row per market x data
-- category) is Architecture-Locked; the actual values in each row
-- (how long, on what legal basis, what happens at expiry) are
-- Legal/Compliance-Configurable: seeded by legal review per market
-- before go-live, never invented by engineering, and re-seedable
-- without a schema change.
-- =====================================================================

CREATE TABLE market_compliance_profiles (
  profile_id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  market_region                    VARCHAR(50) UNIQUE NOT NULL,   -- matches schools.market_region
  applicable_regulations              VARCHAR(50)[] NOT NULL,      -- e.g. {COPPA, FERPA} or a region-specific set
  min_age_without_guardian_consent       SMALLINT NOT NULL DEFAULT 13,  -- the global 13+ floor; a region can lock a stricter value
  created_at                                TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN market_compliance_profiles.min_age_without_guardian_consent IS
  'data_retention_days removed (V3): retention is no longer one flat number per region -- see retention_policies below.';

-- ---------------------------------------------------------------------
-- retention_policies -- structure Architecture-Locked; row VALUES are
-- Legal/Compliance-Configurable.
-- ---------------------------------------------------------------------
CREATE TABLE retention_policies (
  retention_policy_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  market_compliance_profile_id   UUID NOT NULL REFERENCES market_compliance_profiles(profile_id),
  data_category                     VARCHAR(50) NOT NULL,
    -- e.g. STUDENT_IDENTITY | EVIDENCE_EVENTS | MESSAGES | CONDUCT_RECORDS | AI_INVOCATIONS | AUDIT_EVENTS | CONSENT_RECORDS
  retention_period                    INTERVAL NOT NULL,     -- e.g. '7 years', '90 days' -- a real duration, not a bare day-count
  retention_basis                        VARCHAR(50) NOT NULL,  -- e.g. LEGAL_REQUIREMENT, EDUCATIONAL_RECORD, OPERATIONAL_NECESSITY
  action_after_expiry                       VARCHAR(20) NOT NULL
                                               CHECK (action_after_expiry IN ('ANONYMIZE','DELETE','ARCHIVE')),
  created_at                                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (market_compliance_profile_id, data_category)
);

CREATE TABLE consent_records (
  consent_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  guardian_id                 UUID NOT NULL REFERENCES guardian_profiles(guardian_id),
  student_id                     UUID NOT NULL REFERENCES student_profiles(student_id),
  consent_type                      VARCHAR(30) NOT NULL
                                       CHECK (consent_type IN ('DATA_PROCESSING','AI_INTERACTION','COMMUNICATION')),
  market_compliance_profile_id         UUID NOT NULL REFERENCES market_compliance_profiles(profile_id),
  granted_at                              TIMESTAMPTZ NOT NULL,
  revoked_at                                TIMESTAMPTZ,
  created_at                                  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_consent_records_student ON consent_records (student_id);

COMMENT ON TABLE consent_records IS
  'Additive/append-only like student_guardians -- a revoked consent is revoked_at-stamped, never deleted, since proving when consent existed and when it was withdrawn is itself a compliance requirement.';

CREATE TABLE school_agreements (
  agreement_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id             UUID NOT NULL REFERENCES schools(school_id),
  agreement_type           VARCHAR(30) NOT NULL
                              CHECK (agreement_type IN ('DATA_PROCESSING_AGREEMENT','TERMS_OF_SERVICE')),
  signed_by                   UUID NOT NULL REFERENCES users(user_id),
  signed_at                      TIMESTAMPTZ NOT NULL,
  document_ref                     TEXT NOT NULL,
  created_at                         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE data_subject_requests (
  request_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  requested_by             UUID NOT NULL REFERENCES users(user_id),
  subject_person_type         VARCHAR(20) NOT NULL
                                 CHECK (subject_person_type IN ('STUDENT','GUARDIAN')),
  subject_student_id             UUID REFERENCES student_profiles(student_id),
  subject_guardian_id               UUID REFERENCES guardian_profiles(guardian_id),
  request_type                        VARCHAR(20) NOT NULL
                                         CHECK (request_type IN ('ACCESS','DELETION','CORRECTION','PORTABILITY')),
  status                                 VARCHAR(20) NOT NULL
                                            CHECK (status IN ('RECEIVED','IN_PROGRESS','FULFILLED','REJECTED')),
  received_at                               TIMESTAMPTZ NOT NULL,
  fulfilled_at                                 TIMESTAMPTZ,
  created_at                                     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    (subject_person_type = 'STUDENT' AND subject_student_id IS NOT NULL)
    OR
    (subject_person_type = 'GUARDIAN' AND subject_guardian_id IS NOT NULL)
  )
);

CREATE INDEX idx_data_subject_requests_status ON data_subject_requests (status);

COMMENT ON TABLE data_subject_requests IS
  'A DELETION request does not cascade a hard delete through this whole schema. Per the soft-delete convention (§1) and §19.3''s delete strategy, fulfillment is modeled as anonymization/redaction of users/student_profiles/guardian_profiles PII fields, with audit_events recording the fulfillment; the surrounding relationship and evidence history can often be retained in de-identified form, subject to retention_policies (looked up by market_compliance_profile_id and the relevant data_category) and legal review outside this schema''s scope.';
