-- 025_tutor_conversations
-- Applied to Supabase project aayra on 2026-11-01 (version 20261101050502).
--
-- In-lesson AI tutor (docs 10/11 MVP: explain simply, examples/analogies, lesson-grounded doubts,
-- hints and small practice). Conversations are PRIVATE to the student: contract §4 keeps private
-- AI conversations out of every parent/guardian-scoped response, and no teacher endpoint reads them.
-- Only the backend (service role) touches these tables: RLS on, no policies.

CREATE TABLE tutor_conversations (
  conversation_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id              uuid NOT NULL REFERENCES schools(school_id),
  student_id             uuid NOT NULL REFERENCES student_profiles(student_id),
  lesson_id              uuid NOT NULL REFERENCES lessons(lesson_id),
  student_assignment_id  uuid REFERENCES student_assignments(student_assignment_id),
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tutor_conversations_student_lesson ON tutor_conversations (student_id, lesson_id, created_at DESC);

CREATE TABLE tutor_messages (
  message_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id     uuid NOT NULL REFERENCES tutor_conversations(conversation_id),
  role                character varying(10) NOT NULL CHECK (role IN ('STUDENT', 'TUTOR')),
  mode                character varying(20) CHECK (mode IN ('ASK', 'EXPLAIN', 'EXAMPLE', 'HINT', 'PRACTICE')),
  content             text NOT NULL,
  input_mode          character varying(10) NOT NULL DEFAULT 'TEXT' CHECK (input_mode IN ('TEXT', 'VOICE')),
  grounded_chunk_ids  uuid[],
  off_topic           boolean NOT NULL DEFAULT false,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tutor_messages_conversation ON tutor_messages (conversation_id, created_at);

ALTER TABLE tutor_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_messages ENABLE ROW LEVEL SECURITY;

CREATE TRIGGER trg_tutor_conversations_updated_at BEFORE UPDATE ON tutor_conversations
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
