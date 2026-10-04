CREATE SCHEMA IF NOT EXISTS app;
COMMENT ON SCHEMA app IS 'RLS helper functions for the Aayra schema. Not application domain data.';

CREATE OR REPLACE FUNCTION app.active_school_ids()
RETURNS SETOF UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT school_id FROM school_user_roles
  WHERE user_id = auth.uid() AND status = 'ACTIVE';
$$;

CREATE OR REPLACE FUNCTION app.can_view_school(p_school_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT p_school_id IN (SELECT app.active_school_ids());
$$;

CREATE OR REPLACE FUNCTION app.has_active_role(p_school_id UUID, p_role_code TEXT)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM school_user_roles sur
    JOIN roles r ON r.role_id = sur.role_id
    WHERE sur.user_id = auth.uid()
      AND sur.school_id = p_school_id
      AND sur.status = 'ACTIVE'
      AND r.role_code = p_role_code
  );
$$;

CREATE OR REPLACE FUNCTION app.is_school_admin(p_school_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT app.has_active_role(p_school_id,'SCHOOL_ADMIN')
      OR app.has_active_role(p_school_id,'PRINCIPAL');
$$;

CREATE OR REPLACE FUNCTION app.shares_active_school(p_user_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM school_user_roles a
    JOIN school_user_roles b ON a.school_id = b.school_id AND b.status = 'ACTIVE'
    WHERE a.user_id = auth.uid() AND a.status = 'ACTIVE' AND b.user_id = p_user_id
  );
$$;

CREATE OR REPLACE FUNCTION app.current_teacher_id()
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT teacher_id FROM teacher_profiles WHERE user_id = auth.uid() LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION app.current_student_id()
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT student_id FROM student_profiles WHERE user_id = auth.uid() LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION app.current_guardian_id()
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT guardian_id FROM guardian_profiles WHERE user_id = auth.uid() LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION app.teacher_active_section_ids()
RETURNS SETOF UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT section_id FROM teacher_section_subject_assignments
  WHERE teacher_id = app.current_teacher_id() AND status = 'ACTIVE';
$$;

CREATE OR REPLACE FUNCTION app.teacher_student_ids()
RETURNS SETOF UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT DISTINCT se.student_id
  FROM student_enrollments se
  WHERE se.section_id IN (SELECT app.teacher_active_section_ids())
    AND se.status = 'ACTIVE';
$$;

CREATE OR REPLACE FUNCTION app.guardian_student_ids()
RETURNS SETOF UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT student_id FROM student_guardians
  WHERE guardian_id = app.current_guardian_id() AND status = 'ACTIVE';
$$;

CREATE OR REPLACE FUNCTION app.delegate_student_ids()
RETURNS SETOF UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT student_id FROM delegated_guardian_access
  WHERE delegate_user_id = auth.uid() AND revoked_at IS NULL;
$$;

CREATE OR REPLACE FUNCTION app.can_view_student(p_student_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT
    p_student_id = app.current_student_id()
    OR p_student_id IN (SELECT app.guardian_student_ids())
    OR p_student_id IN (SELECT app.delegate_student_ids())
    OR p_student_id IN (SELECT app.teacher_student_ids())
    OR EXISTS (
      SELECT 1 FROM student_profiles sp
      WHERE sp.student_id = p_student_id AND app.is_school_admin(sp.school_id)
    );
$$;

CREATE OR REPLACE FUNCTION app.can_view_guardian(p_guardian_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (SELECT 1 FROM guardian_profiles gp WHERE gp.guardian_id = p_guardian_id AND gp.user_id = auth.uid())
    OR EXISTS (
      SELECT 1 FROM student_guardians sg
      WHERE sg.guardian_id = p_guardian_id AND sg.status = 'ACTIVE'
        AND app.can_view_student(sg.student_id)
    );
$$;

CREATE OR REPLACE FUNCTION app.can_view_lesson(p_lesson_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM lessons l
    WHERE l.lesson_id = p_lesson_id
      AND (
        l.created_by = auth.uid()
        OR app.is_school_admin(l.school_id)
        OR (app.can_view_school(l.school_id) AND l.status IN ('PUBLISHED','ACTIVE','COMPLETED','ARCHIVED'))
      )
  );
$$;

CREATE OR REPLACE FUNCTION app.can_manage_lesson(p_lesson_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM lessons l
    WHERE l.lesson_id = p_lesson_id
      AND (l.created_by = auth.uid() OR app.is_school_admin(l.school_id))
  );
$$;

CREATE OR REPLACE FUNCTION app.content_item_visible(p_content_item_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM content_items ci
    WHERE ci.content_item_id = p_content_item_id
      AND (ci.school_id IS NULL OR app.can_view_school(ci.school_id) OR ci.uploaded_by = auth.uid())
  );
$$;

CREATE OR REPLACE FUNCTION app.can_view_assignment(p_assignment_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM assignments a
    WHERE a.assignment_id = p_assignment_id
      AND (
        a.created_by = auth.uid()
        OR app.is_school_admin(a.school_id)
        OR EXISTS (
          SELECT 1 FROM student_assignments sa
          WHERE sa.assignment_id = a.assignment_id AND app.can_view_student(sa.student_id)
        )
      )
  );
$$;

CREATE OR REPLACE FUNCTION app.can_manage_assignment(p_assignment_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM assignments a
    WHERE a.assignment_id = p_assignment_id
      AND (a.created_by = auth.uid() OR app.is_school_admin(a.school_id))
  );
$$;

CREATE OR REPLACE FUNCTION app.grade_school_id(p_grade_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT school_id FROM grades WHERE grade_id = p_grade_id;
$$;

CREATE OR REPLACE FUNCTION app.tssa_school_id(p_teacher_assignment_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT school_id FROM teacher_section_subject_assignments WHERE teacher_assignment_id = p_teacher_assignment_id;
$$;

CREATE OR REPLACE FUNCTION app.student_assignment_student_id(p_student_assignment_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT student_id FROM student_assignments WHERE student_assignment_id = p_student_assignment_id;
$$;

CREATE OR REPLACE FUNCTION app.student_assignment_assignment_id(p_student_assignment_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT assignment_id FROM student_assignments WHERE student_assignment_id = p_student_assignment_id;
$$;

CREATE OR REPLACE FUNCTION app.submission_student_id(p_submission_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT sa.student_id FROM assignment_submissions s
  JOIN student_assignments sa ON sa.student_assignment_id = s.student_assignment_id
  WHERE s.submission_id = p_submission_id;
$$;

CREATE OR REPLACE FUNCTION app.attempt_student_id(p_attempt_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT student_id FROM assessment_attempts WHERE attempt_id = p_attempt_id;
$$;

CREATE OR REPLACE FUNCTION app.response_student_id(p_response_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT aa.student_id FROM assessment_responses r
  JOIN assessment_attempts aa ON aa.attempt_id = r.assessment_attempt_id
  WHERE r.response_id = p_response_id;
$$;

CREATE OR REPLACE FUNCTION app.review_schedule_student_id(p_review_schedule_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT student_id FROM review_schedules WHERE review_schedule_id = p_review_schedule_id;
$$;

CREATE OR REPLACE FUNCTION app.review_session_student_id(p_review_session_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT student_id FROM review_sessions WHERE review_session_id = p_review_session_id;
$$;

CREATE OR REPLACE FUNCTION app.review_item_teacher_id(p_review_item_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT teacher_id FROM teacher_review_items WHERE review_item_id = p_review_item_id;
$$;

CREATE OR REPLACE FUNCTION app.message_participants_match(p_message_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM parent_teacher_messages m WHERE m.message_id = p_message_id
      AND (m.guardian_id = app.current_guardian_id() OR m.teacher_id = app.current_teacher_id() OR app.is_school_admin(m.school_id))
  );
$$;

CREATE OR REPLACE FUNCTION app.followup_visible(p_followup_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM principal_followups pf WHERE pf.followup_id = p_followup_id
      AND (pf.created_by_principal_id = auth.uid() OR pf.assigned_to_user_id = auth.uid() OR app.is_school_admin(pf.school_id))
  );
$$;

CREATE OR REPLACE FUNCTION app.reward_visible(p_reward_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM rewards r WHERE r.reward_id = p_reward_id
      AND (app.can_view_student(r.student_id) OR r.created_by = auth.uid())
  );
$$;

CREATE OR REPLACE FUNCTION app.batch_school_id(p_batch_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT school_id FROM csv_import_batches WHERE batch_id = p_batch_id;
$$;

CREATE OR REPLACE FUNCTION app.student_guardian_id_of(p_student_guardian_id UUID)
RETURNS UUID LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT guardian_id FROM student_guardians WHERE student_guardian_id = p_student_guardian_id;
$$;

CREATE OR REPLACE FUNCTION app.conduct_record_visible_to_guardian(p_conduct_record_id UUID)
RETURNS BOOLEAN LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM conduct_records cr WHERE cr.conduct_record_id = p_conduct_record_id
      AND cr.visibility = 'GUARDIAN_VISIBLE'
      AND (cr.student_id IN (SELECT app.guardian_student_ids()) OR cr.student_id IN (SELECT app.delegate_student_ids()))
  );
$$;

DO $$
DECLARE f RECORD;
BEGIN
  FOR f IN
    SELECT p.oid::regprocedure AS sig
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'app'
  LOOP
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO authenticated, anon;', f.sig);
  END LOOP;
END;
$$;
