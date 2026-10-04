-- =====================================================================
-- 001_platform_seed.sql
-- Platform-wide reference data: the fixed roles lookup (§2), and
-- market compliance profiles / retention policies (§18) seeded per the
-- Legal/Compliance-Configurable governance tag -- structure is
-- Architecture-Locked (set by the migrations), these row VALUES are a
-- reasonable starting point for a pilot and should be reviewed by
-- legal counsel per market before go-live, exactly as the source
-- document requires.
-- Idempotent: safe to re-run.
-- =====================================================================

-- ---------------------------------------------------------------------
-- roles — five fixed platform primitives (doc 02)
-- ---------------------------------------------------------------------
INSERT INTO roles (role_code) VALUES
  ('STUDENT'),
  ('TEACHER'),
  ('PARENT_GUARDIAN'),
  ('SCHOOL_ADMIN'),
  ('PRINCIPAL')
ON CONFLICT (role_code) DO NOTHING;

-- ---------------------------------------------------------------------
-- market_compliance_profiles — one row per market_region a school can
-- select. Seed with two illustrative markets; extend per actual pilot
-- geographies before go-live.
-- ---------------------------------------------------------------------
INSERT INTO market_compliance_profiles (market_region, applicable_regulations, min_age_without_guardian_consent)
VALUES
  ('US',     ARRAY['COPPA','FERPA'], 13),
  ('GLOBAL', ARRAY['GDPR-K'],        13)
ON CONFLICT (market_region) DO NOTHING;

-- ---------------------------------------------------------------------
-- retention_policies — one row per market x data category. Structure
-- is Architecture-Locked (one row required per category); the
-- retention_period / retention_basis / action_after_expiry VALUES
-- below are placeholders for pilot purposes and must be re-seeded from
-- actual legal review before production go-live in each market.
-- ---------------------------------------------------------------------
INSERT INTO retention_policies (market_compliance_profile_id, data_category, retention_period, retention_basis, action_after_expiry)
SELECT p.profile_id, d.data_category, d.retention_period, d.retention_basis, d.action_after_expiry
FROM market_compliance_profiles p
CROSS JOIN (VALUES
  ('STUDENT_IDENTITY',  INTERVAL '7 years',  'EDUCATIONAL_RECORD',    'ANONYMIZE'),
  ('EVIDENCE_EVENTS',   INTERVAL '5 years',  'EDUCATIONAL_RECORD',    'ANONYMIZE'),
  ('MESSAGES',          INTERVAL '3 years',  'OPERATIONAL_NECESSITY', 'DELETE'),
  ('CONDUCT_RECORDS',   INTERVAL '7 years',  'LEGAL_REQUIREMENT',     'ARCHIVE'),
  ('AI_INVOCATIONS',    INTERVAL '90 days',  'OPERATIONAL_NECESSITY', 'DELETE'),
  ('AUDIT_EVENTS',      INTERVAL '7 years',  'LEGAL_REQUIREMENT',     'ARCHIVE'),
  ('CONSENT_RECORDS',   INTERVAL '10 years', 'LEGAL_REQUIREMENT',     'ARCHIVE')
) AS d(data_category, retention_period, retention_basis, action_after_expiry)
ON CONFLICT (market_compliance_profile_id, data_category) DO NOTHING;
