-- ---------------------------------------------------------------------
-- §15 Rewards & Motivation
-- ---------------------------------------------------------------------
ALTER TABLE rewards ENABLE ROW LEVEL SECURITY;
CREATE POLICY rewards_select ON rewards FOR SELECT TO authenticated USING (app.can_view_student(student_id) OR created_by = auth.uid());
CREATE POLICY rewards_insert ON rewards FOR INSERT TO authenticated WITH CHECK (created_by = auth.uid() AND app.can_view_student(student_id));
CREATE POLICY rewards_update ON rewards FOR UPDATE TO authenticated
  USING (created_by = auth.uid() OR (school_id IS NOT NULL AND app.is_school_admin(school_id)))
  WITH CHECK (created_by = auth.uid() OR (school_id IS NOT NULL AND app.is_school_admin(school_id)));

ALTER TABLE assignment_rewards ENABLE ROW LEVEL SECURITY;
CREATE POLICY ar2_select ON assignment_rewards FOR SELECT TO authenticated USING (app.reward_visible(reward_id));
CREATE POLICY ar2_insert ON assignment_rewards FOR INSERT TO authenticated WITH CHECK (app.reward_visible(reward_id));

-- ---------------------------------------------------------------------
-- §16 Academic Year Lifecycle & Bulk Onboarding — admin-only domain
-- ---------------------------------------------------------------------
ALTER TABLE academic_year_rollovers ENABLE ROW LEVEL SECURITY;
CREATE POLICY ayr_select_admin ON academic_year_rollovers FOR SELECT TO authenticated USING (app.is_school_admin(school_id));
CREATE POLICY ayr_insert_admin ON academic_year_rollovers FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY ayr_update_admin ON academic_year_rollovers FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE csv_import_batches ENABLE ROW LEVEL SECURITY;
CREATE POLICY cib_select_admin ON csv_import_batches FOR SELECT TO authenticated USING (app.is_school_admin(school_id));
CREATE POLICY cib_insert_admin ON csv_import_batches FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id) AND imported_by = auth.uid());

ALTER TABLE csv_import_row_results ENABLE ROW LEVEL SECURITY;
CREATE POLICY cirr_select_admin ON csv_import_row_results FOR SELECT TO authenticated USING (app.is_school_admin(app.batch_school_id(batch_id)));
-- No client INSERT: rows are written by the import-validation backend job.

-- ---------------------------------------------------------------------
-- §17 Analytics, Audit & Platform Operations
-- ---------------------------------------------------------------------
ALTER TABLE class_insight_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY cis_select_staff ON class_insight_snapshots FOR SELECT TO authenticated
  USING (app.is_school_admin(school_id) OR section_id IN (SELECT app.teacher_active_section_ids()));

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY audit_select_admin ON audit_events FOR SELECT TO authenticated USING (school_id IS NOT NULL AND app.is_school_admin(school_id));

-- ai_invocations, background_jobs, domain_event_outbox, ai_generation_runs,
-- ai_context_reference, idempotency_records: deliberately backend-only
-- platform infrastructure (real-time AI call metadata, job orchestration,
-- the event outbox, idempotency guards). RLS enabled, zero policies ->
-- service_role only.
ALTER TABLE ai_invocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE background_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE domain_event_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_generation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_context_reference ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_records ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------
-- §18 Compliance & Consent
-- ---------------------------------------------------------------------
ALTER TABLE market_compliance_profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY mcp_select_all ON market_compliance_profiles FOR SELECT TO authenticated USING (true);

ALTER TABLE retention_policies ENABLE ROW LEVEL SECURITY;
CREATE POLICY rp_select_all ON retention_policies FOR SELECT TO authenticated USING (true);
-- Both platform/legal config; no client writes.

ALTER TABLE consent_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY cr_select ON consent_records FOR SELECT TO authenticated
  USING (guardian_id = app.current_guardian_id() OR EXISTS (SELECT 1 FROM student_profiles sp WHERE sp.student_id = consent_records.student_id AND app.is_school_admin(sp.school_id)));
CREATE POLICY cr_insert ON consent_records FOR INSERT TO authenticated WITH CHECK (guardian_id = app.current_guardian_id());
CREATE POLICY cr_update ON consent_records FOR UPDATE TO authenticated USING (guardian_id = app.current_guardian_id()) WITH CHECK (guardian_id = app.current_guardian_id());

ALTER TABLE school_agreements ENABLE ROW LEVEL SECURITY;
CREATE POLICY sa_select_admin ON school_agreements FOR SELECT TO authenticated USING (app.is_school_admin(school_id));
CREATE POLICY sa_insert_admin ON school_agreements FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id) AND signed_by = auth.uid());

ALTER TABLE data_subject_requests ENABLE ROW LEVEL SECURITY;
CREATE POLICY dsr_select ON data_subject_requests FOR SELECT TO authenticated
  USING (requested_by = auth.uid()
    OR (subject_student_id IS NOT NULL AND app.can_view_student(subject_student_id))
    OR (subject_guardian_id IS NOT NULL AND app.can_view_guardian(subject_guardian_id)));
CREATE POLICY dsr_insert ON data_subject_requests FOR INSERT TO authenticated WITH CHECK (requested_by = auth.uid());

-- ---------------------------------------------------------------------
-- Deferred Appendix — Delegated Guardian Access & Conduct
-- ---------------------------------------------------------------------
ALTER TABLE delegated_guardian_access ENABLE ROW LEVEL SECURITY;
CREATE POLICY dga_select ON delegated_guardian_access FOR SELECT TO authenticated
  USING (delegate_user_id = auth.uid() OR app.student_guardian_id_of(granted_by) = app.current_guardian_id()
    OR EXISTS (SELECT 1 FROM student_profiles sp WHERE sp.student_id = delegated_guardian_access.student_id AND app.is_school_admin(sp.school_id)));
CREATE POLICY dga_insert ON delegated_guardian_access FOR INSERT TO authenticated
  WITH CHECK (app.student_guardian_id_of(granted_by) = app.current_guardian_id());
CREATE POLICY dga_update ON delegated_guardian_access FOR UPDATE TO authenticated
  USING (app.student_guardian_id_of(granted_by) = app.current_guardian_id())
  WITH CHECK (app.student_guardian_id_of(granted_by) = app.current_guardian_id());

ALTER TABLE conduct_records ENABLE ROW LEVEL SECURITY;
CREATE POLICY cr2_select_staff ON conduct_records FOR SELECT TO authenticated
  USING (app.is_school_admin(school_id) OR student_id IN (SELECT app.teacher_student_ids()) OR recorded_by = auth.uid());
CREATE POLICY cr2_select_guardian ON conduct_records FOR SELECT TO authenticated
  USING (visibility = 'GUARDIAN_VISIBLE' AND (student_id = app.current_student_id()
    OR student_id IN (SELECT app.guardian_student_ids()) OR student_id IN (SELECT app.delegate_student_ids())));
CREATE POLICY cr2_insert_staff ON conduct_records FOR INSERT TO authenticated
  WITH CHECK (recorded_by = auth.uid() AND (app.is_school_admin(school_id) OR app.has_active_role(school_id,'TEACHER')));
-- visibility = INTERNAL_ONLY rows are simply never matched by cr2_select_guardian -- enforced here at the
-- database level, mirroring the doc's own instruction that this must never reach a guardian query.

ALTER TABLE conduct_record_guardian_views ENABLE ROW LEVEL SECURITY;
CREATE POLICY crgv_select ON conduct_record_guardian_views FOR SELECT TO authenticated
  USING (guardian_id = app.current_guardian_id()
    OR EXISTS (SELECT 1 FROM conduct_records cr WHERE cr.conduct_record_id = conduct_record_guardian_views.conduct_record_id
      AND (app.is_school_admin(cr.school_id) OR cr.student_id IN (SELECT app.teacher_student_ids()))));
CREATE POLICY crgv_insert ON conduct_record_guardian_views FOR INSERT TO authenticated
  WITH CHECK (guardian_id = app.current_guardian_id() AND app.conduct_record_visible_to_guardian(conduct_record_id));
