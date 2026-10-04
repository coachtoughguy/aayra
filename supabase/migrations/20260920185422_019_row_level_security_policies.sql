-- §2 Identity & Tenancy
ALTER TABLE schools ENABLE ROW LEVEL SECURITY;
CREATE POLICY schools_select_members ON schools FOR SELECT TO authenticated
  USING (school_id IN (SELECT app.active_school_ids()));
CREATE POLICY schools_update_admin ON schools FOR UPDATE TO authenticated
  USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_select_self ON users FOR SELECT TO authenticated
  USING (user_id = auth.uid());
CREATE POLICY users_select_school_directory ON users FOR SELECT TO authenticated
  USING (app.shares_active_school(user_id));
CREATE POLICY users_update_self ON users FOR UPDATE TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

ALTER TABLE roles ENABLE ROW LEVEL SECURITY;
CREATE POLICY roles_select_all ON roles FOR SELECT TO authenticated USING (true);

ALTER TABLE school_user_roles ENABLE ROW LEVEL SECURITY;
CREATE POLICY sur_select_school_roster ON school_user_roles FOR SELECT TO authenticated
  USING (school_id IN (SELECT app.active_school_ids()));
CREATE POLICY sur_insert_admin ON school_user_roles FOR INSERT TO authenticated
  WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY sur_update_admin ON school_user_roles FOR UPDATE TO authenticated
  USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE account_activation_state ENABLE ROW LEVEL SECURITY;
CREATE POLICY aas_select ON account_activation_state FOR SELECT TO authenticated
  USING (
    user_id = auth.uid() OR invited_by = auth.uid()
    OR EXISTS (SELECT 1 FROM school_user_roles sur WHERE sur.user_id = account_activation_state.user_id AND app.is_school_admin(sur.school_id))
  );
CREATE POLICY aas_insert_admin ON account_activation_state FOR INSERT TO authenticated
  WITH CHECK (invited_by = auth.uid());
CREATE POLICY aas_update_admin ON account_activation_state FOR UPDATE TO authenticated
  USING (invited_by = auth.uid() OR user_id = auth.uid())
  WITH CHECK (invited_by = auth.uid() OR user_id = auth.uid());

ALTER TABLE credential_reset_request ENABLE ROW LEVEL SECURITY;
CREATE POLICY crr_select ON credential_reset_request FOR SELECT TO authenticated
  USING (user_id = auth.uid() OR EXISTS (SELECT 1 FROM school_user_roles sur WHERE sur.user_id = credential_reset_request.user_id AND app.is_school_admin(sur.school_id)));
CREATE POLICY crr_insert_self ON credential_reset_request FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

-- §3 School & Academic Structure
ALTER TABLE academic_years ENABLE ROW LEVEL SECURITY;
CREATE POLICY academic_years_select ON academic_years FOR SELECT TO authenticated USING (app.can_view_school(school_id));
CREATE POLICY academic_years_insert_admin ON academic_years FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY academic_years_update_admin ON academic_years FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE grades ENABLE ROW LEVEL SECURITY;
CREATE POLICY grades_select ON grades FOR SELECT TO authenticated USING (app.can_view_school(school_id));
CREATE POLICY grades_insert_admin ON grades FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY grades_update_admin ON grades FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE class_groups ENABLE ROW LEVEL SECURITY;
CREATE POLICY class_groups_select ON class_groups FOR SELECT TO authenticated USING (app.can_view_school(school_id));
CREATE POLICY class_groups_insert_admin ON class_groups FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY class_groups_update_admin ON class_groups FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE sections ENABLE ROW LEVEL SECURITY;
CREATE POLICY sections_select ON sections FOR SELECT TO authenticated USING (app.can_view_school(school_id));
CREATE POLICY sections_insert_admin ON sections FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY sections_update_admin ON sections FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE subjects ENABLE ROW LEVEL SECURITY;
CREATE POLICY subjects_select ON subjects FOR SELECT TO authenticated USING (app.can_view_school(school_id));
CREATE POLICY subjects_insert_admin ON subjects FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY subjects_update_admin ON subjects FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE grade_subjects ENABLE ROW LEVEL SECURITY;
CREATE POLICY grade_subjects_select ON grade_subjects FOR SELECT TO authenticated
  USING (app.can_view_school(app.grade_school_id(grade_id)));
CREATE POLICY grade_subjects_insert_admin ON grade_subjects FOR INSERT TO authenticated
  WITH CHECK (app.is_school_admin(app.grade_school_id(grade_id)));
CREATE POLICY grade_subjects_update_admin ON grade_subjects FOR UPDATE TO authenticated
  USING (app.is_school_admin(app.grade_school_id(grade_id))) WITH CHECK (app.is_school_admin(app.grade_school_id(grade_id)));

-- §4 People, Enrollment & Teaching
ALTER TABLE student_profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY student_profiles_select ON student_profiles FOR SELECT TO authenticated USING (app.can_view_student(student_id));
CREATE POLICY student_profiles_insert_admin ON student_profiles FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY student_profiles_update_admin ON student_profiles FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE teacher_profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY teacher_profiles_select ON teacher_profiles FOR SELECT TO authenticated USING (app.can_view_school(school_id));
CREATE POLICY teacher_profiles_insert_admin ON teacher_profiles FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY teacher_profiles_update ON teacher_profiles FOR UPDATE TO authenticated
  USING (app.is_school_admin(school_id) OR user_id = auth.uid())
  WITH CHECK (app.is_school_admin(school_id) OR user_id = auth.uid());

ALTER TABLE guardian_profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY guardian_profiles_select ON guardian_profiles FOR SELECT TO authenticated USING (app.can_view_guardian(guardian_id));
CREATE POLICY guardian_profiles_insert_self ON guardian_profiles FOR INSERT TO authenticated WITH CHECK (user_id = auth.uid());
CREATE POLICY guardian_profiles_update_self ON guardian_profiles FOR UPDATE TO authenticated USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

ALTER TABLE student_enrollments ENABLE ROW LEVEL SECURITY;
CREATE POLICY student_enrollments_select ON student_enrollments FOR SELECT TO authenticated USING (app.can_view_student(student_id));
CREATE POLICY student_enrollments_insert_admin ON student_enrollments FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY student_enrollments_update_admin ON student_enrollments FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE teacher_section_subject_assignments ENABLE ROW LEVEL SECURITY;
CREATE POLICY tssa_select ON teacher_section_subject_assignments FOR SELECT TO authenticated USING (app.can_view_school(school_id));
CREATE POLICY tssa_insert_admin ON teacher_section_subject_assignments FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY tssa_update_admin ON teacher_section_subject_assignments FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));

ALTER TABLE teacher_assignment_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY tah_select ON teacher_assignment_history FOR SELECT TO authenticated
  USING (app.is_school_admin(app.tssa_school_id(teacher_assignment_id)) OR actor_id = auth.uid());
CREATE POLICY tah_insert ON teacher_assignment_history FOR INSERT TO authenticated
  WITH CHECK (app.is_school_admin(app.tssa_school_id(teacher_assignment_id)) OR actor_id = auth.uid());

-- §5 Guardian & Family
ALTER TABLE student_guardians ENABLE ROW LEVEL SECURITY;
CREATE POLICY student_guardians_select ON student_guardians FOR SELECT TO authenticated
  USING (app.can_view_student(student_id) OR guardian_id = app.current_guardian_id());
CREATE POLICY student_guardians_insert_admin ON student_guardians FOR INSERT TO authenticated WITH CHECK (app.is_school_admin(school_id));
CREATE POLICY student_guardians_update_admin ON student_guardians FOR UPDATE TO authenticated USING (app.is_school_admin(school_id)) WITH CHECK (app.is_school_admin(school_id));
