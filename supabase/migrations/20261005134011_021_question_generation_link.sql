-- 021_question_generation_link
-- Applied to Supabase project aayra on 2026-10-05 (version 20261005134011), approved by Vinay.
--
-- Gap found while implementing contract §6.2/§6.4: `questions` had no link to the generation
-- (assessment_blueprints.version) that produced it, and no place for the answer key/options.
-- Without the link, Review & Publish cannot show the teacher "exactly what version N contains",
-- and an attempt cannot be frozen to its version's question set (§7.4).

ALTER TABLE questions
  ADD COLUMN blueprint_id uuid REFERENCES assessment_blueprints(blueprint_id),
  ADD COLUMN answer_spec  jsonb;

COMMENT ON COLUMN questions.blueprint_id IS
  'Generation (blueprint version) that produced this question. Questions are never edited in place: '
  're-running assessment preparation creates a new blueprint version with its own question rows.';
COMMENT ON COLUMN questions.answer_spec IS
  'Type-specific answer data: MCQ {options:[...], correct_index}, SHORT_ANSWER {accepted:[...]}, '
  'FREE_TEXT {rubric:[...]}. Never sent to students.';

CREATE INDEX idx_questions_blueprint ON questions (blueprint_id) WHERE blueprint_id IS NOT NULL;
