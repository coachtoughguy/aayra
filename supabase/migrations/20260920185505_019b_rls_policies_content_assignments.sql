-- §6.1-6.3 Content, Lessons & AI Content Intelligence
ALTER TABLE lessons ENABLE ROW LEVEL SECURITY;
CREATE POLICY lessons_select ON lessons FOR SELECT TO authenticated
  USING (created_by = auth.uid() OR app.is_school_admin(school_id) OR (app.can_view_school(school_id) AND status IN ('PUBLISHED','ACTIVE','COMPLETED','ARCHIVED')));
CREATE POLICY lessons_insert_staff ON lessons FOR INSERT TO authenticated
  WITH CHECK (created_by = auth.uid() AND (app.has_active_role(school_id,'TEACHER') OR app.is_school_admin(school_id)));
CREATE POLICY lessons_update ON lessons FOR UPDATE TO authenticated
  USING (created_by = auth.uid() OR app.is_school_admin(school_id)) WITH CHECK (created_by = auth.uid() OR app.is_school_admin(school_id));

ALTER TABLE lesson_dependencies ENABLE ROW LEVEL SECURITY;
CREATE POLICY lesson_dependencies_select ON lesson_dependencies FOR SELECT TO authenticated
  USING (app.can_view_lesson(lesson_id) OR app.can_view_lesson(depends_on_lesson_id));
CREATE POLICY lesson_dependencies_insert ON lesson_dependencies FOR INSERT TO authenticated WITH CHECK (app.can_manage_lesson(lesson_id));
CREATE POLICY lesson_dependencies_update ON lesson_dependencies FOR UPDATE TO authenticated USING (app.can_manage_lesson(lesson_id)) WITH CHECK (app.can_manage_lesson(lesson_id));

ALTER TABLE content_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY content_items_select ON content_items FOR SELECT TO authenticated
  USING (school_id IS NULL OR app.can_view_school(school_id) OR uploaded_by = auth.uid());
CREATE POLICY content_items_insert ON content_items FOR INSERT TO authenticated
  WITH CHECK (uploaded_by = auth.uid() AND (school_id IS NULL OR app.can_view_school(school_id)));
CREATE POLICY content_items_update ON content_items FOR UPDATE TO authenticated
  USING (uploaded_by = auth.uid() OR (school_id IS NOT NULL AND app.is_school_admin(school_id)))
  WITH CHECK (uploaded_by = auth.uid() OR (school_id IS NOT NULL AND app.is_school_admin(school_id)));

ALTER TABLE lesson_content ENABLE ROW LEVEL SECURITY;
CREATE POLICY lesson_content_select ON lesson_content FOR SELECT TO authenticated USING (app.can_view_lesson(lesson_id));
CREATE POLICY lesson_content_insert ON lesson_content FOR INSERT TO authenticated WITH CHECK (app.can_manage_lesson(lesson_id));
CREATE POLICY lesson_content_update ON lesson_content FOR UPDATE TO authenticated USING (app.can_manage_lesson(lesson_id)) WITH CHECK (app.can_manage_lesson(lesson_id));

ALTER TABLE content_ownership ENABLE ROW LEVEL SECURITY;
CREATE POLICY content_ownership_select ON content_ownership FOR SELECT TO authenticated USING (app.content_item_visible(content_item_id));
CREATE POLICY content_ownership_insert ON content_ownership FOR INSERT TO authenticated
  WITH CHECK (owner_user_id = auth.uid() OR (owner_school_id IS NOT NULL AND app.is_school_admin(owner_school_id)));

ALTER TABLE content_processing_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY cpj_select ON content_processing_jobs FOR SELECT TO authenticated
  USING (app.is_school_admin(school_id) OR EXISTS (SELECT 1 FROM content_items ci WHERE ci.content_item_id = content_processing_jobs.content_item_id AND ci.uploaded_by = auth.uid()));

ALTER TABLE content_evidence_chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY cec_select_staff ON content_evidence_chunks FOR SELECT TO authenticated
  USING (app.is_school_admin(school_id) OR app.has_active_role(school_id,'TEACHER'));

ALTER TABLE concepts ENABLE ROW LEVEL SECURITY;
CREATE POLICY concepts_select ON concepts FOR SELECT TO authenticated USING (school_id IS NULL OR app.can_view_school(school_id));
CREATE POLICY concepts_insert ON concepts FOR INSERT TO authenticated
  WITH CHECK (school_id IS NOT NULL AND (app.is_school_admin(school_id) OR app.has_active_role(school_id,'TEACHER')));
CREATE POLICY concepts_update ON concepts FOR UPDATE TO authenticated
  USING (school_id IS NOT NULL AND (app.is_school_admin(school_id) OR app.has_active_role(school_id,'TEACHER')))
  WITH CHECK (school_id IS NOT NULL AND (app.is_school_admin(school_id) OR app.has_active_role(school_id,'TEACHER')));

ALTER TABLE lesson_concepts ENABLE ROW LEVEL SECURITY;
CREATE POLICY lesson_concepts_select ON lesson_concepts FOR SELECT TO authenticated USING (app.can_view_lesson(lesson_id));
CREATE POLICY lesson_concepts_insert ON lesson_concepts FOR INSERT TO authenticated WITH CHECK (app.can_manage_lesson(lesson_id));
CREATE POLICY lesson_concepts_update ON lesson_concepts FOR UPDATE TO authenticated USING (app.can_manage_lesson(lesson_id)) WITH CHECK (app.can_manage_lesson(lesson_id));

ALTER TABLE concept_evidence_sources ENABLE ROW LEVEL SECURITY;
CREATE POLICY ces_select_staff ON concept_evidence_sources FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM concepts c WHERE c.concept_id = concept_evidence_sources.concept_id
    AND (c.school_id IS NULL OR app.is_school_admin(c.school_id) OR app.has_active_role(c.school_id,'TEACHER'))));

ALTER TABLE concept_relationships ENABLE ROW LEVEL SECURITY;
CREATE POLICY concept_relationships_select ON concept_relationships FOR SELECT TO authenticated
  USING (EXISTS (SELECT 1 FROM concepts c WHERE c.concept_id = concept_relationships.from_concept_id AND (c.school_id IS NULL OR app.can_view_school(c.school_id))));

ALTER TABLE assessment_blueprints ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment_blueprint_concepts ENABLE ROW LEVEL SECURITY;

-- §7 Assignments & Student Work
ALTER TABLE assignment_source_policies ENABLE ROW LEVEL SECURITY;
CREATE POLICY asp_select_all ON assignment_source_policies FOR SELECT TO authenticated USING (true);

ALTER TABLE assignments ENABLE ROW LEVEL SECURITY;
CREATE POLICY assignments_select ON assignments FOR SELECT TO authenticated
  USING (created_by = auth.uid() OR app.is_school_admin(school_id)
    OR EXISTS (SELECT 1 FROM student_assignments sa WHERE sa.assignment_id = assignments.assignment_id AND app.can_view_student(sa.student_id)));
CREATE POLICY assignments_insert ON assignments FOR INSERT TO authenticated
  WITH CHECK (created_by = auth.uid() AND app.can_view_school(school_id));
CREATE POLICY assignments_update ON assignments FOR UPDATE TO authenticated
  USING (created_by = auth.uid() OR app.is_school_admin(school_id)) WITH CHECK (created_by = auth.uid() OR app.is_school_admin(school_id));

ALTER TABLE assignment_targets ENABLE ROW LEVEL SECURITY;
CREATE POLICY assignment_targets_select ON assignment_targets FOR SELECT TO authenticated USING (app.can_view_assignment(assignment_id));
CREATE POLICY assignment_targets_insert ON assignment_targets FOR INSERT TO authenticated WITH CHECK (app.can_manage_assignment(assignment_id));
CREATE POLICY assignment_targets_update ON assignment_targets FOR UPDATE TO authenticated USING (app.can_manage_assignment(assignment_id)) WITH CHECK (app.can_manage_assignment(assignment_id));

ALTER TABLE assignment_dependencies ENABLE ROW LEVEL SECURITY;
CREATE POLICY assignment_dependencies_select ON assignment_dependencies FOR SELECT TO authenticated
  USING (app.can_view_assignment(assignment_id) OR app.can_view_assignment(prerequisite_assignment_id));
CREATE POLICY assignment_dependencies_insert ON assignment_dependencies FOR INSERT TO authenticated WITH CHECK (app.can_manage_assignment(assignment_id));

ALTER TABLE student_assignments ENABLE ROW LEVEL SECURITY;
CREATE POLICY student_assignments_select ON student_assignments FOR SELECT TO authenticated
  USING (app.can_view_student(student_id) OR app.can_manage_assignment(assignment_id));
CREATE POLICY student_assignments_insert ON student_assignments FOR INSERT TO authenticated WITH CHECK (app.can_manage_assignment(assignment_id));
CREATE POLICY student_assignments_update ON student_assignments FOR UPDATE TO authenticated
  USING (student_id = app.current_student_id() OR app.can_manage_assignment(assignment_id))
  WITH CHECK (student_id = app.current_student_id() OR app.can_manage_assignment(assignment_id));

ALTER TABLE assignment_due_date_exceptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY adde_select ON assignment_due_date_exceptions FOR SELECT TO authenticated
  USING (app.can_view_student(student_id) OR app.can_manage_assignment(assignment_id));
CREATE POLICY adde_insert ON assignment_due_date_exceptions FOR INSERT TO authenticated WITH CHECK (app.can_manage_assignment(assignment_id));

ALTER TABLE assignment_submissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY assignment_submissions_select ON assignment_submissions FOR SELECT TO authenticated
  USING (app.can_view_student(app.student_assignment_student_id(student_assignment_id)) OR app.can_manage_assignment(app.student_assignment_assignment_id(student_assignment_id)));
CREATE POLICY assignment_submissions_insert ON assignment_submissions FOR INSERT TO authenticated
  WITH CHECK (app.student_assignment_student_id(student_assignment_id) = app.current_student_id());
CREATE POLICY assignment_submissions_update ON assignment_submissions FOR UPDATE TO authenticated
  USING (app.can_manage_assignment(app.student_assignment_assignment_id(student_assignment_id)))
  WITH CHECK (app.can_manage_assignment(app.student_assignment_assignment_id(student_assignment_id)));

ALTER TABLE submission_attachments ENABLE ROW LEVEL SECURITY;
CREATE POLICY submission_attachments_select ON submission_attachments FOR SELECT TO authenticated
  USING (app.can_view_student(app.submission_student_id(submission_id)));
CREATE POLICY submission_attachments_insert ON submission_attachments FOR INSERT TO authenticated
  WITH CHECK (app.submission_student_id(submission_id) = app.current_student_id());

-- §8 Focus & Progress
ALTER TABLE focus_cycles ENABLE ROW LEVEL SECURITY;
CREATE POLICY focus_cycles_select ON focus_cycles FOR SELECT TO authenticated USING (app.can_view_student(student_id));
CREATE POLICY focus_cycles_insert ON focus_cycles FOR INSERT TO authenticated WITH CHECK (student_id = app.current_student_id());
CREATE POLICY focus_cycles_update ON focus_cycles FOR UPDATE TO authenticated USING (student_id = app.current_student_id()) WITH CHECK (student_id = app.current_student_id());

ALTER TABLE student_lesson_progress ENABLE ROW LEVEL SECURITY;
CREATE POLICY slp_select ON student_lesson_progress FOR SELECT TO authenticated USING (app.can_view_student(student_id));
CREATE POLICY slp_insert ON student_lesson_progress FOR INSERT TO authenticated WITH CHECK (student_id = app.current_student_id());
CREATE POLICY slp_update ON student_lesson_progress FOR UPDATE TO authenticated USING (student_id = app.current_student_id()) WITH CHECK (student_id = app.current_student_id());

-- §9 Assessment, Evidence & Mastery
ALTER TABLE questions ENABLE ROW LEVEL SECURITY;
CREATE POLICY questions_select ON questions FOR SELECT TO authenticated USING (app.can_view_lesson(lesson_id));
CREATE POLICY questions_insert ON questions FOR INSERT TO authenticated WITH CHECK (app.can_manage_lesson(lesson_id));

ALTER TABLE student_question_exposures ENABLE ROW LEVEL SECURITY;
CREATE POLICY sqe_select ON student_question_exposures FOR SELECT TO authenticated USING (app.can_view_student(student_id));

ALTER TABLE assessment_attempts ENABLE ROW LEVEL SECURITY;
CREATE POLICY aa_select ON assessment_attempts FOR SELECT TO authenticated USING (app.can_view_student(student_id));
CREATE POLICY aa_insert ON assessment_attempts FOR INSERT TO authenticated WITH CHECK (student_id = app.current_student_id());
CREATE POLICY aa_update ON assessment_attempts FOR UPDATE TO authenticated USING (student_id = app.current_student_id()) WITH CHECK (student_id = app.current_student_id());

ALTER TABLE assessment_responses ENABLE ROW LEVEL SECURITY;
CREATE POLICY ar_select ON assessment_responses FOR SELECT TO authenticated USING (app.can_view_student(app.attempt_student_id(assessment_attempt_id)));
CREATE POLICY ar_insert ON assessment_responses FOR INSERT TO authenticated WITH CHECK (app.attempt_student_id(assessment_attempt_id) = app.current_student_id());

ALTER TABLE response_evaluations ENABLE ROW LEVEL SECURITY;
CREATE POLICY re_select ON response_evaluations FOR SELECT TO authenticated USING (app.can_view_student(app.response_student_id(response_id)));
CREATE POLICY re_insert_teacher ON response_evaluations FOR INSERT TO authenticated
  WITH CHECK (evaluated_by = 'TEACHER' AND app.response_student_id(response_id) IN (SELECT app.teacher_student_ids()));

ALTER TABLE evidence_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY evidence_events_select ON evidence_events FOR SELECT TO authenticated USING (app.can_view_student(student_id));

ALTER TABLE student_concept_mastery ENABLE ROW LEVEL SECURITY;
CREATE POLICY scm_select ON student_concept_mastery FOR SELECT TO authenticated USING (app.can_view_student(student_id));

ALTER TABLE mastery_state_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY msh_select ON mastery_state_history FOR SELECT TO authenticated USING (app.can_view_student(student_id));
