-- ---------------------------------------------------------------------
-- §10 Review / Retention Engine
-- ---------------------------------------------------------------------
ALTER TABLE review_schedules ENABLE ROW LEVEL SECURITY;
CREATE POLICY review_schedules_select ON review_schedules FOR SELECT TO authenticated USING (app.can_view_student(student_id));
-- No client writes: schedules are computed by the backend once mastery is reached.

ALTER TABLE review_schedule_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY review_schedule_items_select ON review_schedule_items FOR SELECT TO authenticated
  USING (app.can_view_student(app.review_schedule_student_id(review_schedule_id)));
-- No client writes: scheduled_for/status are backend-computed (§10's own "never mutate in place" rule).

ALTER TABLE review_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY review_sessions_select ON review_sessions FOR SELECT TO authenticated USING (app.can_view_student(student_id));
CREATE POLICY review_sessions_insert ON review_sessions FOR INSERT TO authenticated WITH CHECK (student_id = app.current_student_id());
CREATE POLICY review_sessions_update ON review_sessions FOR UPDATE TO authenticated USING (student_id = app.current_student_id()) WITH CHECK (student_id = app.current_student_id());

ALTER TABLE review_session_questions ENABLE ROW LEVEL SECURITY;
CREATE POLICY rsq_select ON review_session_questions FOR SELECT TO authenticated
  USING (app.can_view_student(app.review_session_student_id(review_session_id)));
CREATE POLICY rsq_insert ON review_session_questions FOR INSERT TO authenticated
  WITH CHECK (app.review_session_student_id(review_session_id) = app.current_student_id());

-- ---------------------------------------------------------------------
-- §11 Teacher Review Queue & Feedback
-- ---------------------------------------------------------------------
ALTER TABLE teacher_review_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY tri_select ON teacher_review_items FOR SELECT TO authenticated
  USING (teacher_id = app.current_teacher_id() OR app.is_school_admin(school_id));
CREATE POLICY tri_update ON teacher_review_items FOR UPDATE TO authenticated
  USING (teacher_id = app.current_teacher_id()) WITH CHECK (teacher_id = app.current_teacher_id());
-- No client INSERT: queue items are raised automatically (low AI confidence / subjective response).

ALTER TABLE evaluation_adjustment ENABLE ROW LEVEL SECURITY;
CREATE POLICY ea_select ON evaluation_adjustment FOR SELECT TO authenticated
  USING (adjusted_by = app.current_teacher_id() OR app.review_item_teacher_id(review_item_id) = app.current_teacher_id());
CREATE POLICY ea_insert ON evaluation_adjustment FOR INSERT TO authenticated
  WITH CHECK (adjusted_by = app.current_teacher_id() AND app.review_item_teacher_id(review_item_id) = app.current_teacher_id());

ALTER TABLE teacher_feedback ENABLE ROW LEVEL SECURITY;
CREATE POLICY tf_select_teacher ON teacher_feedback FOR SELECT TO authenticated USING (teacher_id = app.current_teacher_id());
CREATE POLICY tf_select_student ON teacher_feedback FOR SELECT TO authenticated
  USING (status = 'PUBLISHED' AND visible_to_student AND app.can_view_student(student_id));
CREATE POLICY tf_insert ON teacher_feedback FOR INSERT TO authenticated WITH CHECK (teacher_id = app.current_teacher_id());
CREATE POLICY tf_update ON teacher_feedback FOR UPDATE TO authenticated USING (teacher_id = app.current_teacher_id()) WITH CHECK (teacher_id = app.current_teacher_id());

-- ---------------------------------------------------------------------
-- §12 Personalization / Learning DNA — internal, staff-only (never
-- surfaced directly to guardian/student per the domain's own doc note).
-- ---------------------------------------------------------------------
ALTER TABLE learning_observations ENABLE ROW LEVEL SECURITY;
CREATE POLICY lo_select_staff ON learning_observations FOR SELECT TO authenticated
  USING (student_id IN (SELECT app.teacher_student_ids()) OR EXISTS (SELECT 1 FROM student_profiles sp WHERE sp.student_id = learning_observations.student_id AND app.is_school_admin(sp.school_id)));

ALTER TABLE learning_hypotheses ENABLE ROW LEVEL SECURITY;
CREATE POLICY lh_select_staff ON learning_hypotheses FOR SELECT TO authenticated
  USING (student_id IN (SELECT app.teacher_student_ids()) OR EXISTS (SELECT 1 FROM student_profiles sp WHERE sp.student_id = learning_hypotheses.student_id AND app.is_school_admin(sp.school_id)));

ALTER TABLE personalization_interventions ENABLE ROW LEVEL SECURITY;
CREATE POLICY pi_select_staff ON personalization_interventions FOR SELECT TO authenticated
  USING (student_id IN (SELECT app.teacher_student_ids()) OR EXISTS (SELECT 1 FROM student_profiles sp WHERE sp.student_id = personalization_interventions.student_id AND app.is_school_admin(sp.school_id)));
-- No client writes anywhere in §12: this whole domain is backend-computed (observe -> hypothesize -> personalize loop).

-- ---------------------------------------------------------------------
-- §13 Communication, Escalation & Follow-ups
-- ---------------------------------------------------------------------
ALTER TABLE parent_teacher_messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY ptm_select ON parent_teacher_messages FOR SELECT TO authenticated
  USING (guardian_id = app.current_guardian_id() OR teacher_id = app.current_teacher_id() OR app.is_school_admin(school_id));
CREATE POLICY ptm_insert ON parent_teacher_messages FOR INSERT TO authenticated
  WITH CHECK (guardian_id = app.current_guardian_id() OR teacher_id = app.current_teacher_id());
CREATE POLICY ptm_update ON parent_teacher_messages FOR UPDATE TO authenticated
  USING (guardian_id = app.current_guardian_id() OR teacher_id = app.current_teacher_id())
  WITH CHECK (guardian_id = app.current_guardian_id() OR teacher_id = app.current_teacher_id());

ALTER TABLE parent_teacher_message_replies ENABLE ROW LEVEL SECURITY;
CREATE POLICY ptmr_select ON parent_teacher_message_replies FOR SELECT TO authenticated USING (app.message_participants_match(message_id));
CREATE POLICY ptmr_insert ON parent_teacher_message_replies FOR INSERT TO authenticated
  WITH CHECK (sender_user_id = auth.uid() AND app.message_participants_match(message_id));

ALTER TABLE parent_attention_cases ENABLE ROW LEVEL SECURITY;
CREATE POLICY pac_select_staff ON parent_attention_cases FOR SELECT TO authenticated
  USING (app.is_school_admin(school_id) OR student_id IN (SELECT app.teacher_student_ids()));
CREATE POLICY pac_select_guardian ON parent_attention_cases FOR SELECT TO authenticated
  USING (status IN ('SENT_TO_PARENT','RESOLVED') AND app.can_view_student(student_id));
CREATE POLICY pac_insert_staff ON parent_attention_cases FOR INSERT TO authenticated
  WITH CHECK (app.is_school_admin(school_id) OR app.has_active_role(school_id,'TEACHER'));
CREATE POLICY pac_update_staff ON parent_attention_cases FOR UPDATE TO authenticated
  USING (reviewed_by_teacher_id = app.current_teacher_id() OR student_id IN (SELECT app.teacher_student_ids()) OR app.is_school_admin(school_id))
  WITH CHECK (reviewed_by_teacher_id = app.current_teacher_id() OR student_id IN (SELECT app.teacher_student_ids()) OR app.is_school_admin(school_id));
-- Note the deliberate gap: a guardian can never SELECT a PROPOSED/TEACHER_APPROVED/NOT_REQUIRED row —
-- this is the schema's teacher-approval gate (§13) enforced physically, not just in application code.

ALTER TABLE principal_followups ENABLE ROW LEVEL SECURITY;
CREATE POLICY pf_select ON principal_followups FOR SELECT TO authenticated
  USING (created_by_principal_id = auth.uid() OR assigned_to_user_id = auth.uid() OR app.is_school_admin(school_id));
CREATE POLICY pf_insert ON principal_followups FOR INSERT TO authenticated
  WITH CHECK (created_by_principal_id = auth.uid() AND app.has_active_role(school_id,'PRINCIPAL'));
CREATE POLICY pf_update ON principal_followups FOR UPDATE TO authenticated
  USING (created_by_principal_id = auth.uid() OR assigned_to_user_id = auth.uid() OR app.is_school_admin(school_id))
  WITH CHECK (created_by_principal_id = auth.uid() OR assigned_to_user_id = auth.uid() OR app.is_school_admin(school_id));

ALTER TABLE followup_status_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY fsh_select ON followup_status_history FOR SELECT TO authenticated USING (app.followup_visible(followup_id));
CREATE POLICY fsh_insert ON followup_status_history FOR INSERT TO authenticated
  WITH CHECK (changed_by = auth.uid() AND app.followup_visible(followup_id));

ALTER TABLE teacher_response ENABLE ROW LEVEL SECURITY;
CREATE POLICY tr_select ON teacher_response FOR SELECT TO authenticated USING (app.followup_visible(followup_id));
CREATE POLICY tr_insert ON teacher_response FOR INSERT TO authenticated
  WITH CHECK (teacher_id = app.current_teacher_id() AND app.followup_visible(followup_id));

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
CREATE POLICY notifications_select ON notifications FOR SELECT TO authenticated USING (recipient_user_id = auth.uid());
CREATE POLICY notifications_update ON notifications FOR UPDATE TO authenticated USING (recipient_user_id = auth.uid()) WITH CHECK (recipient_user_id = auth.uid());
-- No client INSERT: notifications are system-generated projections.;
