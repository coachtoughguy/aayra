-- =====================================================================
-- 001_identity_and_tenancy.sql
-- Domain: Identity & Tenancy (Physical Data Model §2)
-- Tables: schools, users, roles, school_user_roles,
--         account_activation_state, credential_reset_request
-- =====================================================================

-- ---------------------------------------------------------------------
-- schools — tenant root
-- ---------------------------------------------------------------------
CREATE TABLE schools (
  school_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                VARCHAR(200) NOT NULL,
  school_code         VARCHAR(50) NOT NULL UNIQUE,
  market_region       VARCHAR(50) NOT NULL,        -- feeds market_compliance_profiles, §18
  timezone            VARCHAR(50) NOT NULL,
  country_code        CHAR(2) NOT NULL,
  status              VARCHAR(20) NOT NULL DEFAULT 'SETUP'
                        CHECK (status IN ('SETUP','ACTIVE','SUSPENDED','ARCHIVED')),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE schools IS 'Tenant root. Every school-scoped table carries school_id directly or transitively.';

-- ---------------------------------------------------------------------
-- users — one login-capable identity per human, regardless of how many
-- roles/schools they hold (doc 02: one-person/one-account model)
-- ---------------------------------------------------------------------
CREATE TABLE users (
  user_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email               CITEXT UNIQUE,
  phone               VARCHAR(30),
  display_name        VARCHAR(200) NOT NULL,
  date_of_birth       DATE,                         -- only ever populated for a role_membership of STUDENT; null otherwise
  age_gate_status     VARCHAR(30) NOT NULL DEFAULT 'not_applicable'
                        CHECK (age_gate_status IN ('not_applicable','verified_13_plus','blocked_under_13')),
  account_status      VARCHAR(20) NOT NULL DEFAULT 'INVITED'
                        CHECK (account_status IN ('INVITED','ACTIVE','LOCKED','DISABLED','ARCHIVED')),
  last_login_at       TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN users.date_of_birth IS 'Only ever populated for a role_membership of STUDENT; null otherwise.';
COMMENT ON COLUMN users.account_status IS
  'A sub-13 student (age_gate_status = blocked_under_13) is still a users row -- it must be, since student_profiles.user_id and every enrollment/mastery/evidence FK downstream needs a stable identity to point at -- but account_status for that row never leaves a non-authenticating state, and no credential ever issues; all student-facing product actions route through student_guardians (§5) at the application layer.';

-- ---------------------------------------------------------------------
-- roles — small fixed lookup table, NOT school-configurable (unlike
-- subjects, §3) -- the five roles are fixed platform primitives.
-- Modeled as a lookup table (not a bare enum/CHECK) because
-- audit_events.actor_role and reporting need to join against it.
-- ---------------------------------------------------------------------
CREATE TABLE roles (
  role_id             SMALLSERIAL PRIMARY KEY,
  role_code           VARCHAR(30) NOT NULL UNIQUE   -- STUDENT | TEACHER | PARENT_GUARDIAN | SCHOOL_ADMIN | PRINCIPAL
);

-- ---------------------------------------------------------------------
-- school_user_roles — role membership of a user at a school
-- ---------------------------------------------------------------------
CREATE TABLE school_user_roles (
  school_user_role_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  user_id             UUID NOT NULL REFERENCES users(user_id),
  role_id             SMALLINT NOT NULL REFERENCES roles(role_id),
  status              VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                        CHECK (status IN ('ACTIVE','INACTIVE')),
  created_by          UUID REFERENCES users(user_id),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (school_id, user_id, role_id)
);

COMMENT ON TABLE school_user_roles IS
  'A Principal who also guardians a child, or a Teacher who is also a Parent, holds more than one row here without duplicate logins.';

CREATE INDEX idx_school_user_roles_school ON school_user_roles (school_id);
CREATE INDEX idx_school_user_roles_user ON school_user_roles (user_id);

-- ---------------------------------------------------------------------
-- account_activation_state — per-invite timeline (CSV bulk-invite flow)
-- invite_sent -> invite_opened -> credentials_set -> activated -> invite_expired
-- kept as a separate history-shaped table rather than folded into
-- users.account_status alone, because that would lose the per-invite
-- timeline.
-- ---------------------------------------------------------------------
CREATE TABLE account_activation_state (
  activation_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES users(user_id),
  state               VARCHAR(20) NOT NULL
                        CHECK (state IN ('invite_sent','invite_opened','credentials_set','activated','invite_expired')),
  invited_by          UUID NOT NULL REFERENCES users(user_id),
  invite_sent_at      TIMESTAMPTZ NOT NULL,
  expires_at          TIMESTAMPTZ NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_account_activation_state_user ON account_activation_state (user_id);

-- ---------------------------------------------------------------------
-- credential_reset_request
-- ---------------------------------------------------------------------
CREATE TABLE credential_reset_request (
  reset_request_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id             UUID NOT NULL REFERENCES users(user_id),
  method              VARCHAR(20) NOT NULL
                        CHECK (method IN ('email','phone','admin_reset')),
  status              VARCHAR(20) NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','completed','expired')),
  requested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_credential_reset_request_user ON credential_reset_request (user_id);
