-- 22 API & Event Contract v1.2, section 10: seven additive prerequisites for Slice 1/2.
-- User approved applying directly to main (free plan, empty database), 2026-10-04.

-- 1. One ACTIVE enrollment per student per academic year (allows TRANSFERRED history rows).
ALTER TABLE public.student_enrollments
  DROP CONSTRAINT student_enrollments_student_id_academic_year_id_key;
CREATE UNIQUE INDEX student_enrollments_active_uq
  ON public.student_enrollments (student_id, academic_year_id)
  WHERE status = 'ACTIVE';

-- 2. Reviews reuse the shared assessment/evaluation pipeline.
ALTER TABLE public.assessment_attempts
  ADD COLUMN review_session_id uuid REFERENCES public.review_sessions(review_session_id);
CREATE INDEX idx_assessment_attempts_review_session
  ON public.assessment_attempts (review_session_id)
  WHERE review_session_id IS NOT NULL;

-- 3. Explicit processing-failure lesson state.
ALTER TABLE public.lessons DROP CONSTRAINT lessons_status_check;
ALTER TABLE public.lessons ADD CONSTRAINT lessons_status_check
  CHECK (status IN ('DRAFT','PROCESSING','PROCESSING_FAILED','READY',
                    'SCHEDULED','PUBLISHED','ACTIVE','COMPLETED','ARCHIVED'));

-- 4. Optimistic concurrency on lessons.
ALTER TABLE public.lessons ADD COLUMN version integer NOT NULL DEFAULT 1;

-- 5. Learning completion vs. required submission artifact.
ALTER TABLE public.assignments ADD COLUMN completion_mode character varying
  NOT NULL DEFAULT 'LEARNING_COMPLETION'
  CONSTRAINT assignments_completion_mode_check
  CHECK (completion_mode IN ('LEARNING_COMPLETION','SUBMISSION_REQUIRED','BOTH'));

-- 6. Evidence eligibility frozen at write time.
ALTER TABLE public.evidence_events
  ADD COLUMN mastery_eligible boolean NOT NULL DEFAULT true,
  ADD COLUMN review_eligible  boolean NOT NULL DEFAULT true;

-- 7. Non-authoritative client telemetry for focus-cycle duration.
ALTER TABLE public.focus_cycles ADD COLUMN client_reported_seconds integer;
