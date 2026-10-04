-- =====================================================================
-- 999_updated_at_triggers.sql
-- Wires the shared set_updated_at() trigger function (defined in
-- 000_extensions_and_helpers.sql) to every table in the public schema
-- that has an updated_at column, so created_at/updated_at NOT NULL
-- (doc §1 convention) is enforced consistently without hand-listing
-- ~70 CREATE TRIGGER statements (and risking missing one).
--
-- Append-only / history / infrastructure tables that deliberately have
-- no updated_at column (evidence_events, mastery_state_history,
-- audit_events, ai_invocations, domain_event_outbox,
-- idempotency_records, concept_evidence_sources,
-- conduct_record_guardian_views, roles, followup_status_history,
-- teacher_assignment_history's created_at-only siblings, etc.) are
-- automatically skipped by the information_schema check below.
-- =====================================================================

DO $$
DECLARE
  t RECORD;
BEGIN
  FOR t IN
    SELECT c.table_name
    FROM information_schema.columns c
    WHERE c.table_schema = 'public'
      AND c.column_name = 'updated_at'
      AND EXISTS (
        SELECT 1 FROM information_schema.tables tb
        WHERE tb.table_schema = 'public'
          AND tb.table_name = c.table_name
          AND tb.table_type = 'BASE TABLE'
      )
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_trigger
      WHERE tgname = 'trg_' || t.table_name || '_set_updated_at'
    ) THEN
      EXECUTE format(
        'CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION set_updated_at();',
        'trg_' || t.table_name || '_set_updated_at',
        t.table_name
      );
    END IF;
  END LOOP;
END;
$$;
