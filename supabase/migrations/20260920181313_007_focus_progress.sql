-- =====================================================================
-- 007_focus_progress.sql
-- Domain: Learning Activity — Focus & Progress (Physical Data Model §8)
-- Tables: focus_cycles, student_lesson_progress
-- Splits the raw Take Break / Continue Learning / Finish Lesson
-- timeline (focus_cycles) from the higher-level "where is this student
-- in this lesson" state (student_lesson_progress): a focus cycle is
-- just elapsed-time bookkeeping, never a session boundary, and it
-- explicitly feeds Learning DNA (§12) without being mastery evidence
-- itself.
-- =====================================================================

CREATE TABLE focus_cycles (
  focus_cycle_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id              UUID NOT NULL REFERENCES student_profiles(student_id),
  lesson_id                UUID NOT NULL REFERENCES lessons(lesson_id),
  student_assignment_id    UUID REFERENCES student_assignments(student_assignment_id),
  started_at               TIMESTAMPTZ NOT NULL,
  ended_at                 TIMESTAMPTZ,
  planned_minutes          SMALLINT,
  actual_seconds           INTEGER,
  end_reason               VARCHAR(30)
                              CHECK (end_reason IN ('BREAK','CONTINUE_LATER','FINISH_LESSON','INTERRUPTED')),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_focus_cycles_student_lesson ON focus_cycles (student_id, lesson_id);

COMMENT ON TABLE focus_cycles IS
  'focus_cycle_id rows are never deleted; a student redoing a lesson simply accumulates more rows, keeping total_active_seconds and the mastery evidence from each pass (§9) independently auditable.';

CREATE TABLE student_lesson_progress (
  progress_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  student_id               UUID NOT NULL REFERENCES student_profiles(student_id),
  lesson_id                 UUID NOT NULL REFERENCES lessons(lesson_id),
  student_assignment_id     UUID REFERENCES student_assignments(student_assignment_id),
  status                    VARCHAR(20) NOT NULL DEFAULT 'NOT_STARTED'
                               CHECK (status IN ('NOT_STARTED','IN_PROGRESS','LEARNING_COMPLETE','ASSESSING','COMPLETED')),
  progress_metadata         JSONB,
  first_started_at          TIMESTAMPTZ,
  last_activity_at          TIMESTAMPTZ,
  finished_learning_at      TIMESTAMPTZ,
  total_active_seconds      INTEGER NOT NULL DEFAULT 0,   -- accumulated across every focus cycle, a running counter shown in the UI -- never a fixed target
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (student_id, lesson_id, student_assignment_id)
);

CREATE INDEX idx_student_lesson_progress_student_status ON student_lesson_progress (student_id, status, last_activity_at);

COMMENT ON COLUMN student_lesson_progress.status IS
  'Moves NOT_STARTED -> IN_PROGRESS -> LEARNING_COMPLETE -> ASSESSING -> COMPLETED; the ASSESSING state is what lets the UI distinguish "still reading the material" from "now working the end-of-lesson mastery check," both of which the old session model conflated into one screen.';
