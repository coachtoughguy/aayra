"""Teacher review & edit of AI-generated lesson content before publishing (docs 11/12, LOCKED:
"Teacher preview is mandatory before student publication. Teacher can review ... and make
appropriate edits before Publish"; Sept 16: Review & Publish requires genuine teacher review).

Generations stay immutable (contract §6.4): every edit makes a NEW generation version — a copy of
the current one with the change applied — and supersedes the old one. So:
- the AI's original version stays in history (provenance on each question says what it came from);
- publish still names the exact `reviewed_generation_version` the teacher looked at, and a stale
  version is rejected, so two teachers editing at once can't publish something nobody reviewed;
- every edit requests the `generation_version` it was made against (optimistic concurrency).

Editing is only possible while the lesson is READY. Once published, content is locked (doc 21).
`POST /lessons/{id}/regenerate` asks the AI for a fresh generation (edits are kept in history).
"""

import json
import re
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app import audit, errors
from app.auth import Actor, current_actor
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one
from app.idempotency import OpClass, run_idempotent
from app.outbox import emit
from app.services.pipeline import enqueue_lesson_preparation
from app.services.teaching import load_lesson_for_teacher

router = APIRouter(prefix="/v1", tags=["lesson-editing"])

MIN_CORRECT = {"CORE": 2, "SUPPORTING": 1, "STRETCH": 1, "PREREQUISITE": 1}
CognitiveType = Literal["RECALL", "UNDERSTANDING", "REASONING", "APPLICATION", "TRANSFER"]
Importance = Literal["CORE", "SUPPORTING", "STRETCH", "PREREQUISITE"]
Difficulty = Literal["EASY", "MEDIUM", "HARD"]


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", name.lower())).strip()


# ------------------------------------------------------------------------------------------------
# Copy-on-write generation
# ------------------------------------------------------------------------------------------------
async def _editable(conn: AsyncConnection, actor: Actor, lesson_id: UUID, generation_version: int) -> tuple[dict, dict]:
    lesson, teacher = await load_lesson_for_teacher(conn, actor, lesson_id, lock=True)
    if lesson["status"] != "READY":
        raise errors.AppError(
            "LESSON_NOT_EDITABLE", 409,
            "Generated content can be edited while the lesson is ready for review; published content is locked.",
            {"status": lesson["status"]},
        )  # fmt: skip
    gen = await fetch_one(
        conn, "SELECT * FROM assessment_blueprints WHERE lesson_id = :l AND status = 'ACTIVE'", l=lesson_id
    )
    if gen is None or gen["version"] != generation_version:
        raise errors.generation_version_stale(
            reviewed_generation_version=generation_version, current_generation_version=gen["version"] if gen else None
        )
    return lesson, {**gen, "teacher_id": teacher.teacher_id}


async def _fork(conn: AsyncConnection, actor: Actor, lesson: dict, gen: dict, change: dict) -> tuple[UUID, int, dict]:
    """New ACTIVE generation N+1 copied from N. Returns (blueprint_id, version, {old_question_id: new})."""
    run = await fetch_one(
        conn,
        """INSERT INTO ai_generation_runs (school_id, run_type, entity_type, entity_id, model_name, prompt_version,
                                           status, input_summary, started_at, completed_at)
           VALUES (:s, 'TEACHER_EDIT', 'lesson', :l, 'teacher', NULL, 'SUCCEEDED', CAST(:inp AS jsonb), now(), now())
           RETURNING generation_run_id""",
        s=lesson["school_id"], l=lesson["lesson_id"],
        inp=json.dumps({"from_version": gen["version"], "edited_by": str(actor.user_id), **change}, default=str),
    )  # fmt: skip
    version = gen["version"] + 1
    await execute(
        conn, "UPDATE assessment_blueprints SET status = 'SUPERSEDED' WHERE blueprint_id = :b", b=gen["blueprint_id"]
    )
    bp = await fetch_one(
        conn,
        """INSERT INTO assessment_blueprints (lesson_id, generation_run_id, version, status, generated_at)
           VALUES (:l, :r, :v, 'ACTIVE', now()) RETURNING blueprint_id""",
        l=lesson["lesson_id"], r=run["generation_run_id"], v=version,
    )  # fmt: skip
    new_bp = bp["blueprint_id"]
    await execute(
        conn,
        """INSERT INTO assessment_blueprint_concepts (blueprint_id, concept_id, required_evidence, priority)
           SELECT :nb, concept_id, required_evidence, priority FROM assessment_blueprint_concepts WHERE blueprint_id = :ob""",
        nb=new_bp, ob=gen["blueprint_id"],
    )  # fmt: skip
    qmap: dict = {}
    for q in await fetch_all(conn, "SELECT * FROM questions WHERE blueprint_id = :b ORDER BY created_at, question_id",
                             b=gen["blueprint_id"]):  # fmt: skip
        nid = uuid4()
        qmap[q["question_id"]] = nid
        prov = {**(q["source_provenance"] or {}), "blueprint_version": version, "copied_from": str(q["question_id"])}
        await execute(
            conn,
            """INSERT INTO questions (question_id, lesson_id, concept_id, blueprint_id, question_type, cognitive_type,
                                      prompt_text, difficulty, answer_spec, generated_by_model, source_provenance,
                                      created_at)
               VALUES (:id, :l, :c, :b, :qt, :ct, :p, :d, CAST(:a AS jsonb), :m, CAST(:prov AS jsonb), :at)""",
            id=nid, l=q["lesson_id"], c=q["concept_id"], b=new_bp, qt=q["question_type"], ct=q["cognitive_type"],
            p=q["prompt_text"], d=q["difficulty"], a=json.dumps(q["answer_spec"]), m=q["generated_by_model"],
            prov=json.dumps(prov), at=q["created_at"],
        )  # fmt: skip
    return new_bp, version, qmap


async def _finish(
    conn: AsyncConnection, ctx: RequestContext, actor: Actor, lesson: dict, version: int, change: dict
) -> dict:
    row = await fetch_one(
        conn, "UPDATE lessons SET version = version + 1 WHERE lesson_id = :l RETURNING version", l=lesson["lesson_id"]
    )
    await audit.record(
        conn, ctx, action="LESSON_GENERATION_EDITED", entity_type="lesson", entity_id=lesson["lesson_id"],
        school_id=lesson["school_id"], actor_user_id=actor.user_id, actor_role="TEACHER",
        after={"generation_version": version, **change},
    )  # fmt: skip
    await emit(
        conn, ctx, event_type="LessonGenerationEdited", aggregate_type="lesson", aggregate_id=lesson["lesson_id"],
        school_id=lesson["school_id"], data={"generation_version": version, **change},
    )  # fmt: skip
    return {"lesson_id": lesson["lesson_id"], "lesson_version": row["version"], "generation_version": version}


async def _concept_questions(conn: AsyncConnection, blueprint_id: UUID, concept_id: UUID) -> int:
    row = await fetch_one(
        conn,
        "SELECT count(*) AS n FROM questions WHERE blueprint_id = :b AND concept_id = :c",
        b=blueprint_id,
        c=concept_id,
    )
    return row["n"]


# ------------------------------------------------------------------------------------------------
# Questions
# ------------------------------------------------------------------------------------------------
class AnswerFields(BaseModel):
    options: list[str] | None = Field(default=None, min_length=2, max_length=6)
    correct_index: int | None = Field(default=None, ge=0, le=5)
    rubric: list[str] | None = Field(default=None, min_length=1, max_length=6)
    hint: str | None = Field(default=None, max_length=1000)
    explanation: str | None = Field(default=None, max_length=2000)


def _answer_spec(question_type: str, base: dict, fields: AnswerFields) -> dict:
    spec = dict(base or {})
    for k in ("options", "correct_index", "rubric", "hint", "explanation"):
        v = getattr(fields, k)
        if v is not None:
            spec[k] = [x.strip() for x in v] if isinstance(v, list) else (v.strip() if isinstance(v, str) else v)
    if question_type == "MCQ":
        opts = spec.get("options") or []
        if len(opts) < 2 or any(not o for o in opts) or len(set(o.lower() for o in opts)) != len(opts):
            raise errors.validation_error("A multiple-choice question needs 2-6 different, non-empty options.")
        if not isinstance(spec.get("correct_index"), int) or not 0 <= spec["correct_index"] < len(opts):
            raise errors.validation_error("Mark which option is correct.", field="correct_index")
        spec.pop("rubric", None)
    else:
        if not spec.get("rubric") or any(not r for r in spec["rubric"]):
            raise errors.validation_error("A written-answer question needs 1-6 rubric points.", field="rubric")
        spec.pop("options", None)
        spec.pop("correct_index", None)
    return spec


class QuestionEdit(AnswerFields):
    generation_version: int = Field(ge=1)
    prompt_text: str | None = Field(default=None, min_length=5, max_length=2000)
    difficulty: Difficulty | None = None
    cognitive_type: CognitiveType | None = None


@router.patch("/lessons/{lesson_id}/questions/{question_id}")
async def edit_question(
    lesson_id: UUID, question_id: UUID, body: QuestionEdit, request: Request,
    actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async def handler(conn: AsyncConnection):
        lesson, gen = await _editable(conn, actor, lesson_id, body.generation_version)
        q = await fetch_one(conn, "SELECT * FROM questions WHERE question_id = :q AND blueprint_id = :b",
                            q=question_id, b=gen["blueprint_id"])  # fmt: skip
        if q is None:
            raise errors.not_found("question", question_id=str(question_id))
        spec = _answer_spec(q["question_type"], q["answer_spec"], body)
        new_bp, version, qmap = await _fork(conn, actor, lesson, gen, {"edit": "question", "question_id": question_id})
        await execute(
            conn,
            """UPDATE questions SET prompt_text = :p, difficulty = :d, cognitive_type = :ct, answer_spec = CAST(:a AS jsonb),
                      source_provenance = source_provenance || CAST(:prov AS jsonb)
                WHERE question_id = :q""",
            p=(body.prompt_text or q["prompt_text"]).strip(), d=body.difficulty or q["difficulty"],
            ct=body.cognitive_type or q["cognitive_type"], a=json.dumps(spec),
            prov=json.dumps({"edited_by_teacher": True}), q=qmap[question_id],
        )  # fmt: skip
        result = await _finish(conn, ctx, actor, lesson, version, {"edit": "question"})
        return 200, {**result, "question_id": qmap[question_id]}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT)


class QuestionNew(AnswerFields):
    generation_version: int = Field(ge=1)
    concept_id: UUID
    question_type: Literal["MCQ", "FREE_TEXT"]
    cognitive_type: CognitiveType
    prompt_text: str = Field(min_length=5, max_length=2000)
    difficulty: Difficulty = "MEDIUM"


@router.post("/lessons/{lesson_id}/questions", status_code=201)
async def add_question(
    lesson_id: UUID, body: QuestionNew, request: Request,
    actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async def handler(conn: AsyncConnection):
        lesson, gen = await _editable(conn, actor, lesson_id, body.generation_version)
        in_scope = await fetch_one(
            conn, "SELECT 1 AS x FROM assessment_blueprint_concepts WHERE blueprint_id = :b AND concept_id = :c",
            b=gen["blueprint_id"], c=body.concept_id,
        )  # fmt: skip
        if in_scope is None:
            raise errors.not_found("concept", concept_id=str(body.concept_id))
        spec = _answer_spec(body.question_type, {}, body)
        new_bp, version, _ = await _fork(conn, actor, lesson, gen, {"edit": "question_added"})
        qid = uuid4()
        await execute(
            conn,
            """INSERT INTO questions (question_id, lesson_id, concept_id, blueprint_id, question_type, cognitive_type,
                                      prompt_text, difficulty, answer_spec, generated_by_model, source_provenance)
               VALUES (:id, :l, :c, :b, :qt, :ct, :p, :d, CAST(:a AS jsonb), 'teacher', CAST(:prov AS jsonb))""",
            id=qid, l=lesson_id, c=body.concept_id, b=new_bp, qt=body.question_type, ct=body.cognitive_type,
            p=body.prompt_text.strip(), d=body.difficulty, a=json.dumps(spec),
            prov=json.dumps({"written_by_teacher": True, "blueprint_version": version}),
        )  # fmt: skip
        result = await _finish(conn, ctx, actor, lesson, version, {"edit": "question_added"})
        return 201, {**result, "question_id": qid}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT)


@router.delete("/lessons/{lesson_id}/questions/{question_id}")
async def remove_question(
    lesson_id: UUID, question_id: UUID, generation_version: int, request: Request,
    actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async def handler(conn: AsyncConnection):
        lesson, gen = await _editable(conn, actor, lesson_id, generation_version)
        q = await fetch_one(conn, "SELECT * FROM questions WHERE question_id = :q AND blueprint_id = :b",
                            q=question_id, b=gen["blueprint_id"])  # fmt: skip
        if q is None:
            raise errors.not_found("question", question_id=str(question_id))
        if await _concept_questions(conn, gen["blueprint_id"], q["concept_id"]) <= 1:
            raise errors.AppError("CONCEPT_NEEDS_QUESTION", 409,
                                  "Each topic needs at least one question. Add another first, or remove the topic.")  # fmt: skip
        new_bp, version, qmap = await _fork(
            conn, actor, lesson, gen, {"edit": "question_removed", "question_id": question_id}
        )
        await execute(conn, "UPDATE questions SET blueprint_id = NULL WHERE question_id = :q", q=qmap[question_id])
        return 200, await _finish(conn, ctx, actor, lesson, version, {"edit": "question_removed"})

    return await run_idempotent(
        request,
        actor,
        {"remove_question": str(question_id), "v": generation_version},
        handler,
        op_class=OpClass.CONTENT,
    )


# ------------------------------------------------------------------------------------------------
# Concepts (the lesson's topics)
# ------------------------------------------------------------------------------------------------
class ConceptEdit(BaseModel):
    generation_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=2, max_length=250)
    description: str | None = Field(default=None, max_length=2000)
    importance: Importance | None = None

    @model_validator(mode="after")
    def _something(self):
        if self.name is None and self.description is None and self.importance is None:
            raise ValueError("nothing to change")
        return self


@router.patch("/lessons/{lesson_id}/concepts/{concept_id}")
async def edit_concept(
    lesson_id: UUID, concept_id: UUID, body: ConceptEdit, request: Request,
    actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async def handler(conn: AsyncConnection):
        lesson, gen = await _editable(conn, actor, lesson_id, body.generation_version)
        current = await fetch_one(
            conn,
            """SELECT c.*, lc.importance FROM lesson_concepts lc JOIN concepts c USING (concept_id)
                WHERE lc.lesson_id = :l AND lc.concept_id = :c""",
            l=lesson_id, c=concept_id,
        )  # fmt: skip
        if current is None:
            raise errors.not_found("concept", concept_id=str(concept_id))
        new_bp, version, _ = await _fork(conn, actor, lesson, gen, {"edit": "concept", "concept_id": concept_id})
        target = concept_id
        name = body.name.strip() if body.name else None
        if name and _normalize(name) != current["normalized_name"]:
            # Concepts are shared per subject; renaming here must not rename it in other lessons, so
            # this lesson switches to the concept with the new name (reused or created).
            existing = await fetch_one(
                conn, "SELECT concept_id FROM concepts WHERE subject_id = :s AND school_id = :sc AND normalized_name = :n",
                s=lesson["subject_id"], sc=lesson["school_id"], n=_normalize(name),
            )  # fmt: skip
            if existing and await fetch_one(conn, "SELECT 1 AS x FROM lesson_concepts WHERE lesson_id = :l AND concept_id = :c",
                                            l=lesson_id, c=existing["concept_id"]):  # fmt: skip
                raise errors.AppError("CONCEPT_EXISTS", 409, "This lesson already has a topic with that name.")
            target = existing["concept_id"] if existing else uuid4()
            if not existing:
                await execute(
                    conn,
                    """INSERT INTO concepts (concept_id, school_id, subject_id, name, normalized_name, description)
                       VALUES (:id, :sc, :s, :n, :nn, :d)""",
                    id=target, sc=lesson["school_id"], s=lesson["subject_id"], n=name, nn=_normalize(name),
                    d=body.description if body.description is not None else current["description"],
                )  # fmt: skip
            await execute(conn, "UPDATE lesson_concepts SET concept_id = :t WHERE lesson_id = :l AND concept_id = :c",
                          t=target, l=lesson_id, c=concept_id)  # fmt: skip
            await execute(conn, "UPDATE assessment_blueprint_concepts SET concept_id = :t WHERE blueprint_id = :b AND concept_id = :c",
                          t=target, b=new_bp, c=concept_id)  # fmt: skip
            await execute(conn, "UPDATE questions SET concept_id = :t WHERE blueprint_id = :b AND concept_id = :c",
                          t=target, b=new_bp, c=concept_id)  # fmt: skip
        elif body.description is not None:
            # A topic's description is shared metadata for that subject's concept.
            await execute(
                conn, "UPDATE concepts SET description = :d WHERE concept_id = :c", d=body.description, c=concept_id
            )
        if body.importance:
            await execute(conn, "UPDATE lesson_concepts SET importance = :i WHERE lesson_id = :l AND concept_id = :c",
                          i=body.importance, l=lesson_id, c=target)  # fmt: skip
            await execute(
                conn,
                """UPDATE assessment_blueprint_concepts
                      SET required_evidence = required_evidence || CAST(:req AS jsonb)
                    WHERE blueprint_id = :b AND concept_id = :c""",
                req=json.dumps({"min_correct": MIN_CORRECT[body.importance]}), b=new_bp, c=target,
            )  # fmt: skip
        result = await _finish(conn, ctx, actor, lesson, version, {"edit": "concept"})
        return 200, {**result, "concept_id": target}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT)


@router.delete("/lessons/{lesson_id}/concepts/{concept_id}")
async def remove_concept(
    lesson_id: UUID, concept_id: UUID, generation_version: int, request: Request,
    actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async def handler(conn: AsyncConnection):
        lesson, gen = await _editable(conn, actor, lesson_id, generation_version)
        scope = await fetch_all(conn, "SELECT concept_id FROM assessment_blueprint_concepts WHERE blueprint_id = :b",
                                b=gen["blueprint_id"])  # fmt: skip
        if concept_id not in {c["concept_id"] for c in scope}:
            raise errors.not_found("concept", concept_id=str(concept_id))
        if len(scope) <= 1:
            raise errors.AppError("LAST_CONCEPT", 409, "A lesson needs at least one topic.")
        new_bp, version, _ = await _fork(
            conn, actor, lesson, gen, {"edit": "concept_removed", "concept_id": concept_id}
        )
        await execute(conn, "DELETE FROM assessment_blueprint_concepts WHERE blueprint_id = :b AND concept_id = :c",
                      b=new_bp, c=concept_id)  # fmt: skip
        await execute(conn, "UPDATE questions SET blueprint_id = NULL WHERE blueprint_id = :b AND concept_id = :c",
                      b=new_bp, c=concept_id)  # fmt: skip
        await execute(
            conn, "DELETE FROM lesson_concepts WHERE lesson_id = :l AND concept_id = :c", l=lesson_id, c=concept_id
        )
        return 200, await _finish(conn, ctx, actor, lesson, version, {"edit": "concept_removed"})

    return await run_idempotent(
        request, actor, {"remove_concept": str(concept_id), "v": generation_version}, handler, op_class=OpClass.CONTENT
    )


# ------------------------------------------------------------------------------------------------
# Regenerate
# ------------------------------------------------------------------------------------------------
class RegenerateIn(BaseModel):
    version: int = Field(ge=1)


@router.post("/lessons/{lesson_id}/regenerate")
async def regenerate(
    lesson_id: UUID, body: RegenerateIn, request: Request,
    actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async def handler(conn: AsyncConnection):
        lesson, _ = await load_lesson_for_teacher(conn, actor, lesson_id, lock=True)
        if lesson["version"] != body.version:
            raise errors.version_conflict(entity="lessons", id=str(lesson_id), expected_version=body.version,
                                          current_version=lesson["version"])  # fmt: skip
        if lesson["status"] != "READY":
            raise errors.AppError("LESSON_NOT_EDITABLE", 409, "Only a lesson waiting for review can be regenerated.",
                                  {"status": lesson["status"]})  # fmt: skip
        row = await fetch_one(
            conn,
            "UPDATE lessons SET status = 'PROCESSING', version = version + 1 WHERE lesson_id = :l RETURNING version",
            l=lesson_id,
        )
        await enqueue_lesson_preparation(conn, lesson["school_id"], lesson_id)
        await emit(conn, ctx, event_type="LessonRegenerationRequested", aggregate_type="lesson", aggregate_id=lesson_id,
                   school_id=lesson["school_id"], data={"lesson_id": lesson_id})  # fmt: skip
        return 200, {"lesson_id": lesson_id, "status": "PROCESSING", "version": row["version"]}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT)
