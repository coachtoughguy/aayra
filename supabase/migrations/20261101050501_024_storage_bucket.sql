-- 024_storage_bucket
-- Applied to Supabase project aayra on 2026-11-01 (version 20261101050501).
--
-- The private Storage bucket the API uses for lesson material, transcripts and submissions
-- (AAYRA_STORAGE_BUCKET, default "lesson-content"). Private: files are only reachable through
-- short-lived signed URLs the API hands out; the browser never gets a public link.
-- Guarded so it is a no-op on databases without Supabase Storage (local test databases).
DO $$
BEGIN
  IF to_regclass('storage.buckets') IS NOT NULL THEN
    INSERT INTO storage.buckets (id, name, public)
    VALUES ('lesson-content', 'lesson-content', false)
    ON CONFLICT (id) DO UPDATE SET public = false;
  END IF;
END
$$;
