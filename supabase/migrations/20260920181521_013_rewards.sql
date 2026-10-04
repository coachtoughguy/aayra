-- =====================================================================
-- 013_rewards.sql
-- Domain: Rewards & Motivation (Physical Data Model §15)
-- Tables: rewards, assignment_rewards
-- Doc 17 is SEED-status and deliberately defers exact reward
-- mechanics. A reward as an optional capability reservation, sourced
-- from Parent/Teacher/Platform; a dedicated redemption table isn't yet
-- justified by any locked mechanic.
-- =====================================================================

CREATE TABLE rewards (
  reward_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID REFERENCES schools(school_id),   -- null for a platform-sourced reward
  created_by             UUID NOT NULL REFERENCES users(user_id),
  student_id                UUID NOT NULL REFERENCES student_profiles(student_id),
  source_type                  VARCHAR(20) NOT NULL
                                  CHECK (source_type IN ('PARENT','TEACHER','PLATFORM')),
  title                          VARCHAR(200) NOT NULL,   -- "Pick movie night film"
  description                      TEXT,
  status                             VARCHAR(20) NOT NULL DEFAULT 'RESERVED'
                                        CHECK (status IN ('RESERVED','EARNED','REDEEMED','EXPIRED')),
  created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_rewards_student_status ON rewards (student_id, status);

COMMENT ON TABLE rewards IS
  '"Capability reservation": status = RESERVED from creation, visible before it''s earned -- not a payout that only appears after the fact. A student sees a reward in the UI only when a rewards row exists -- no reward record, no reward UI; there is deliberately no implicit or default reward.';

CREATE TABLE assignment_rewards (
  assignment_reward_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  assignment_id            UUID NOT NULL REFERENCES assignments(assignment_id),
  reward_id                   UUID NOT NULL REFERENCES rewards(reward_id),
  condition_payload              JSONB,   -- e.g. {"trigger": "assignment_completed", "by": "2026-09-20"}
  created_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (assignment_id, reward_id)
);

COMMENT ON TABLE assignment_rewards IS
  'rewards.status transitions (RESERVED -> EARNED -> REDEEMED) are driven by condition_payload resolution against student_assignments/evidence_events, never by a direct UI write to status -- keeps the unlock logic server-side and auditable. Exact catalog mechanics (a school-wide store, a points economy) remain explicitly out of scope per doc 17''s SEED status; this table set supports only the single-reward-per-assignment pattern actually shown in the Parent wireframes.';
