-- =====================================================================
-- 000_extensions_and_helpers.sql
-- Aayra Physical Data Model — Extensions, conventions, and shared helpers
-- Source: Aayra — Physical Data Model (v3), Sept 2026
-- =====================================================================
-- Conventions (doc §1):
--   * snake_case, singular table names
--   * <table>_id UUID PK DEFAULT gen_random_uuid()
--   * FK columns named <referenced_table_singular>_id; role-qualified
--     FKs are prefixed for clarity (e.g. primary_teacher_id)
--   * status/type columns are VARCHAR + CHECK unless the value set is
--     itself school-configurable data (subjects, grades) or a closed
--     platform-wide set referenced from many tables (roles, as a
--     lookup table)
--   * every school-scoped table carries school_id directly or
--     transitively
--   * created_at / updated_at TIMESTAMPTZ NOT NULL on every table
--   * soft-delete via status / effective_to / unlinked_at / archived_at
--     columns — never DELETE on anything with educational or audit
--     significance (see §19.3 in the source doc for the full policy)
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid() belt-and-braces (built in since PG13, harmless if already core)
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email matching (users.email)

-- ---------------------------------------------------------------------
-- Shared updated_at trigger function.
-- Applied to every table via 999_updated_at_triggers.sql once all
-- tables exist, rather than repeating a trigger statement per table.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

COMMENT ON FUNCTION set_updated_at() IS
  'Generic BEFORE UPDATE trigger: stamps updated_at = now() on every row change. Wired to every table with an updated_at column by 999_updated_at_triggers.sql.';
