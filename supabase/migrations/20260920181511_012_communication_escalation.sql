-- =====================================================================
-- 012_communication_escalation.sql
-- Domain: Communication, Escalation & Follow-ups (Physical Data Model
-- §13)
-- Tables: parent_teacher_messages, parent_teacher_message_replies,
--         parent_attention_cases, principal_followups,
--         followup_status_history, teacher_response, notifications
-- parent_attention_cases: doc 20's Sept 15 addendum requires teacher
-- approval before any AI-detected pattern reaches a Parent or
-- Principal -- status flow PROPOSED -> TEACHER_APPROVED/NOT_REQUIRED
-- -> SENT_TO_PARENT -> RESOLVED, explicitly with no AUTO_SENT state.
-- principal_followups: Open -> Acknowledged -> Planned -> Completed
-- with a Teacher Response and full Status History, matching the
-- reviewed and approved Principal/Teacher wireframes.
-- =====================================================================

CREATE TABLE parent_teacher_messages (
  message_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id           UUID NOT NULL REFERENCES schools(school_id),
  student_id             UUID NOT NULL REFERENCES student_profiles(student_id),
  guardian_id               UUID NOT NULL REFERENCES guardian_profiles(guardian_id),
  teacher_id                  UUID NOT NULL REFERENCES teacher_profiles(teacher_id),
  lesson_id                     UUID REFERENCES lessons(lesson_id),
  assignment_id                   UUID REFERENCES assignments(assignment_id),
  topic_type                        VARCHAR(30) NOT NULL
                                       CHECK (topic_type IN ('QUESTION','EXTENSION_REQUEST','LEARNING_SUPPORT','OTHER_ACADEMIC')),
  message_text                        TEXT NOT NULL,
  status                                VARCHAR(20) NOT NULL,
  created_at                             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_parent_teacher_messages_teacher_status ON parent_teacher_messages (teacher_id, status);
CREATE INDEX idx_parent_teacher_messages_guardian ON parent_teacher_messages (guardian_id);

COMMENT ON TABLE parent_teacher_messages IS
  'Structured, not free-chat: backs Parent PA-11 Message Teacher and Teacher TR-13 Parent Message Detail.';

CREATE TABLE parent_teacher_message_replies (
  reply_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id           UUID NOT NULL REFERENCES parent_teacher_messages(message_id),
  sender_user_id           UUID NOT NULL REFERENCES users(user_id),
  reply_text                 TEXT NOT NULL,
  created_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_parent_teacher_message_replies_message ON parent_teacher_message_replies (message_id);

CREATE TABLE parent_attention_cases (
  case_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id               UUID NOT NULL REFERENCES schools(school_id),
  student_id                 UUID NOT NULL REFERENCES student_profiles(student_id),
  concept_id                   UUID REFERENCES concepts(concept_id),
  assignment_id                   UUID REFERENCES assignments(assignment_id),
  proposed_by                       VARCHAR(20) NOT NULL
                                       CHECK (proposed_by IN ('AI','TEACHER')),
  reason_code                         VARCHAR(50) NOT NULL,
  reason_summary                        TEXT NOT NULL,
  supporting_evidence                     JSONB,   -- references into evidence_events, never raw prompts
  status                                    VARCHAR(30) NOT NULL
                                               CHECK (status IN ('PROPOSED','TEACHER_APPROVED','NOT_REQUIRED','SENT_TO_PARENT','RESOLVED')),
  proposed_at                                TIMESTAMPTZ NOT NULL,
  reviewed_by_teacher_id                       UUID REFERENCES teacher_profiles(teacher_id),
  reviewed_at                                    TIMESTAMPTZ,
  teacher_reason                                   TEXT,
  resolved_at                                        TIMESTAMPTZ,
  version                                              INTEGER NOT NULL DEFAULT 1,   -- optimistic concurrency (Architecture-Locked, V3)
  created_at                                            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_parent_attention_cases_student_status ON parent_attention_cases (student_id, status);

COMMENT ON TABLE parent_attention_cases IS
  'Teacher-approval gate: only a TEACHER_APPROVED case may ever produce a notification to a guardian. Deliberately no AUTO_SENT status -- status-visibility (teacher/school seeing raw signals) and escalation-notification (a parent being told) are separate per doc 03''s AI Architecture boundaries; this table enforces that separation physically.';

CREATE TABLE principal_followups (
  followup_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id                 UUID NOT NULL REFERENCES schools(school_id),
  created_by_principal_id      UUID NOT NULL REFERENCES users(user_id),
  assigned_to_user_id             UUID NOT NULL REFERENCES users(user_id),
  section_id                        UUID REFERENCES sections(section_id),   -- nullable: some follow-ups are class-level, not student-specific
  subject_id                          UUID REFERENCES subjects(subject_id),
  student_id                            UUID REFERENCES student_profiles(student_id),
  title                                   VARCHAR(250) NOT NULL,
  issue_summary                             TEXT NOT NULL,
  supporting_evidence                         JSONB,
  suggested_action                              TEXT,
  status                                          VARCHAR(20) NOT NULL DEFAULT 'OPEN'
                                                     CHECK (status IN ('OPEN','ACKNOWLEDGED','PLANNED','COMPLETED','CANCELLED')),
  completed_at                                      TIMESTAMPTZ,
  version                                             INTEGER NOT NULL DEFAULT 1,   -- optimistic concurrency (Architecture-Locked, V3)
  created_at                                           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_principal_followups_assigned_status ON principal_followups (assigned_to_user_id, status);

CREATE TABLE followup_status_history (
  history_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  followup_id        UUID NOT NULL REFERENCES principal_followups(followup_id),
  from_status           VARCHAR(20),
  to_status               VARCHAR(20) NOT NULL,
  changed_by                UUID NOT NULL REFERENCES users(user_id),
  comment                     TEXT,    -- distinct from teacher_response below: an audit note, not the always-visible reply
  changed_at                    TIMESTAMPTZ NOT NULL,
  created_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_followup_status_history_followup ON followup_status_history (followup_id);

CREATE TABLE teacher_response (
  response_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  followup_id         UUID NOT NULL REFERENCES principal_followups(followup_id),
  teacher_id             UUID NOT NULL REFERENCES teacher_profiles(teacher_id),
  response_text            TEXT NOT NULL,
  responded_at                TIMESTAMPTZ NOT NULL,
  created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_teacher_response_followup ON teacher_response (followup_id);

COMMENT ON TABLE teacher_response IS
  'A distinct, always-visible artifact in the UI, kept separate from the terser status-history comment field.';

CREATE TABLE notifications (
  notification_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id              UUID NOT NULL REFERENCES schools(school_id),
  recipient_user_id         UUID NOT NULL REFERENCES users(user_id),
  notification_type           VARCHAR(50) NOT NULL,
    -- ASSIGNMENT_PUBLISHED | DUE_SOON | OVERDUE | REVIEW_DUE | TEACHER_FEEDBACK | PARENT_ATTENTION |
    -- PRINCIPAL_FOLLOWUP | MESSAGE_RECEIVED | FOLLOW_UP_STATUS_CHANGE
  title                        VARCHAR(250) NOT NULL,
  body                           TEXT,
  priority                        VARCHAR(20),
  entity_type                       VARCHAR(50),
  entity_id                           UUID,      -- polymorphic pointer to the triggering row
  deep_link                             TEXT,
  status                                  VARCHAR(20) NOT NULL DEFAULT 'UNREAD'
                                             CHECK (status IN ('UNREAD','READ','ARCHIVED')),
  read_at                                   TIMESTAMPTZ,
  expires_at                                  TIMESTAMPTZ,
  created_at                                    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                                     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_notifications_recipient_status_created ON notifications (recipient_user_id, status, created_at DESC);

COMMENT ON TABLE notifications IS
  'Notifications are projections; they are never the source of truth for the underlying event. Every principal_followups.status transition MUST produce a followup_status_history row -- enforced with a trigger or at the application layer, so the status column and the history table can never drift. conduct_record (§14, Deferred) deliberately has no FK from principal_followups -- see §14 and §19 for why.';
