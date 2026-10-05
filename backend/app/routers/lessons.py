"""Contract §6.1 (lesson + attach), §6.3 (dependencies), §6.4 (Review & Publish), §6.5 (TR-07 progress)."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.auth import Actor, current_actor
from app.concurrency import update_versioned
from app.context import RequestContext, get_context
from app.db import constraint_name, execute, fetch_all, fetch_one, get_db
from app.idempotency import OpClass, run_idempotent
from app.outbox import emit
from app.services.distribution import distribution_status
from app.services.pipeline import enqueue_lesson_preparation, item_extraction_state
from app.services.teaching import (
    load_lesson_for_teacher,
    require_subject_authority,
    teaches_section_subject,
)

router = APIRouter(prefix="/v1", tags=["lessons"])

EDITABLE = ("DRAFT", "PROCESSING", "PROCESSING_FAILED", "READY")
PUBLISHABLE = ("READY", "PUBLISHED", "ACTIVE")
PROCESSING_VIEW = {
    None: "NOT_STARTED",
    "QUEUED": "QUEUED",
    "RETRYING": "PROCESSING",
    "PROCESSING": "PROCESSING",
    "SUCCEEDED": "DONE",
    "FAILED": "FAILED",
    "CANCELLED": "FAILED",
}


# ------------------------------------------------------------------------------------------------
# POST /v1/lessons
# ------------------------------------------------------------------------------------------------
class LessonIn(BaseModel):
    subject_id: UUID
    academic_year_id: UUID
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    instructions: str | None = None


@router.post("/lessons", status_code=201)
async def create_lesson(
    body: LessonIn, request: Request, actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context)
) -> JSONResponse:
    async with get_db().connect() as conn:
        teacher = await require_subject_authority(conn, actor, body.subject_id, body.academic_year_id)
    request.state.school_id = str(teacher.school_id)

    async def handler(conn: AsyncConnection):
        row = await fetch_one(
            conn,
            """INSERT INTO lessons (school_id, academic_year_id, subject_id, created_by, title, description,
                                    instructions, status, requires_teacher_approval)
               VALUES (:s, :y, :sub, :u, :t, :d, :i, 'DRAFT', true)
               RETURNING lesson_id, status, version""",
            s=teacher.school_id,
            y=body.academic_year_id,
            sub=body.subject_id,
            u=actor.user_id,
            t=body.title,
            d=body.description,
            i=body.instructions,
        )
        await emit(
            conn,
            ctx,
            event_type="LessonCreated",
            aggregate_type="lesson",
            aggregate_id=row["lesson_id"],
            school_id=teacher.school_id,
            data={"lesson_id": row["lesson_id"], "subject_id": body.subject_id, "title": body.title},
        )
        return 201, {
            **row,
            "title": body.title,
            "subject_id": body.subject_id,
            "academic_year_id": body.academic_year_id,
        }

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT, school_id=teacher.school_id)


# ------------------------------------------------------------------------------------------------
# POST /v1/lessons/{id}/content
# ------------------------------------------------------------------------------------------------
class AttachIn(BaseModel):
    content_item_id: UUID
    display_order: int = Field(ge=0, le=1000)


@router.post("/lessons/{lesson_id}/content", status_code=201)
async def attach_content(
    lesson_id: UUID,
    body: AttachIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        lesson, teacher = await load_lesson_for_teacher(conn, actor, lesson_id)
    request.state.school_id = str(lesson["school_id"])

    async def handler(conn: AsyncConnection):
        locked = await fetch_one(conn, "SELECT status FROM lessons WHERE lesson_id = :l FOR UPDATE", l=lesson_id)
        if locked["status"] not in EDITABLE:
            raise errors.AppError(
                "LESSON_CONTENT_LOCKED", 409, "Published lesson content cannot change.", {"status": locked["status"]}
            )
        item = await fetch_one(
            conn, "SELECT * FROM content_items WHERE content_item_id = :c AND school_id = :s",
            c=body.content_item_id, s=lesson["school_id"],
        )  # fmt: skip
        if item is None:
            raise errors.not_found("content_item", content_item_id=str(body.content_item_id))
        if not (item["object_key"] or item["source_url"]):
            raise errors.AppError(
                "CONTENT_NOT_UPLOADED", 409, "Finish uploading this file before attaching it to a lesson."
            )
        try:
            async with conn.begin_nested():
                await execute(
                    conn,
                    "INSERT INTO lesson_content (lesson_id, content_item_id, display_order) VALUES (:l, :c, :o)",
                    l=lesson_id,
                    c=body.content_item_id,
                    o=body.display_order,
                )
        except IntegrityError as exc:
            if constraint_name(exc) == "lesson_content_lesson_id_content_item_id_key":
                raise errors.AppError(
                    "CONTENT_ALREADY_ATTACHED", 409, "This content is already on the lesson."
                ) from exc
            raise
        # Any change to attached content invalidates the current generation: back to PROCESSING.
        row = await fetch_one(
            conn,
            """UPDATE lessons SET status = 'PROCESSING', version = version + 1 WHERE lesson_id = :l
               RETURNING status, version""",
            l=lesson_id,
        )
        state = await item_extraction_state(conn, body.content_item_id)
        if state in ("SUCCEEDED", "FAILED"):
            # Reused item already processed: only re-run lesson-level stages. (A previously failed item
            # makes preparation mark the lesson PROCESSING_FAILED; the teacher can retry or remove it.)
            await enqueue_lesson_preparation(conn, lesson["school_id"], lesson_id)
        await emit(
            conn,
            ctx,
            event_type="LessonContentAttached",
            aggregate_type="lesson",
            aggregate_id=lesson_id,
            school_id=lesson["school_id"],
            data={"lesson_id": lesson_id, "content_item_id": body.content_item_id, "reused": state == "SUCCEEDED"},
        )
        return 201, {"lesson_id": lesson_id, "content_item_id": body.content_item_id, **row}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT, school_id=lesson["school_id"])


# ------------------------------------------------------------------------------------------------
# DELETE /v1/lessons/{id}/content/{content_item_id} — remove a file (e.g. one that can't be read)
# ------------------------------------------------------------------------------------------------
@router.delete("/lessons/{lesson_id}/content/{content_item_id}")
async def detach_content(
    lesson_id: UUID,
    content_item_id: UUID,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        lesson, _ = await load_lesson_for_teacher(conn, actor, lesson_id)
    request.state.school_id = str(lesson["school_id"])

    async def handler(conn: AsyncConnection):
        locked = await fetch_one(conn, "SELECT status FROM lessons WHERE lesson_id = :l FOR UPDATE", l=lesson_id)
        if locked["status"] not in EDITABLE:
            raise errors.AppError(
                "LESSON_CONTENT_LOCKED", 409, "Published lesson content cannot change.", {"status": locked["status"]}
            )
        removed = await execute(
            conn,
            "DELETE FROM lesson_content WHERE lesson_id = :l AND content_item_id = :c",
            l=lesson_id,
            c=content_item_id,
        )
        if not removed:
            raise errors.not_found("lesson_content", content_item_id=str(content_item_id))
        remaining = await fetch_one(conn, "SELECT count(*) AS n FROM lesson_content WHERE lesson_id = :l", l=lesson_id)
        new_status = "PROCESSING" if remaining["n"] else "DRAFT"
        row = await fetch_one(
            conn,
            "UPDATE lessons SET status = :st, version = version + 1 WHERE lesson_id = :l RETURNING status, version",
            st=new_status,
            l=lesson_id,
        )
        if remaining["n"]:
            await enqueue_lesson_preparation(conn, lesson["school_id"], lesson_id)
        await emit(
            conn,
            ctx,
            event_type="LessonContentDetached",
            aggregate_type="lesson",
            aggregate_id=lesson_id,
            school_id=lesson["school_id"],
            data={"lesson_id": lesson_id, "content_item_id": content_item_id},
        )
        return 200, {"lesson_id": lesson_id, "content_item_id": content_item_id, **row}

    return await run_idempotent(
        request,
        actor,
        {"detach": str(content_item_id)},
        handler,
        op_class=OpClass.CONTENT,
        school_id=lesson["school_id"],
    )


# ------------------------------------------------------------------------------------------------
# POST /v1/lessons/{id}/dependencies (§6.3)
# ------------------------------------------------------------------------------------------------
class DependencyIn(BaseModel):
    depends_on_lesson_id: UUID


@router.post("/lessons/{lesson_id}/dependencies", status_code=201)
async def add_dependency(
    lesson_id: UUID,
    body: DependencyIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    if body.depends_on_lesson_id == lesson_id:
        raise errors.validation_error("A lesson cannot depend on itself.")
    async with get_db().connect() as conn:
        lesson, _ = await load_lesson_for_teacher(conn, actor, lesson_id)
        other = await fetch_one(conn, "SELECT school_id FROM lessons WHERE lesson_id = :l", l=body.depends_on_lesson_id)
    if other is None or other["school_id"] != lesson["school_id"]:
        raise errors.not_found("lesson", lesson_id=str(body.depends_on_lesson_id))

    async def handler(conn: AsyncConnection):
        cycle = await fetch_one(
            conn,
            """WITH RECURSIVE up(id) AS (
                   SELECT depends_on_lesson_id FROM lesson_dependencies WHERE lesson_id = :other
                   UNION SELECT d.depends_on_lesson_id FROM lesson_dependencies d JOIN up ON d.lesson_id = up.id)
               SELECT 1 AS x FROM up WHERE id = :me LIMIT 1""",
            other=body.depends_on_lesson_id,
            me=lesson_id,
        )
        if cycle:
            raise errors.AppError("DEPENDENCY_CYCLE", 409, "That prerequisite would create a cycle.")
        await execute(
            conn,
            """INSERT INTO lesson_dependencies (lesson_id, depends_on_lesson_id) VALUES (:l, :d)
               ON CONFLICT (lesson_id, depends_on_lesson_id) DO NOTHING""",
            l=lesson_id,
            d=body.depends_on_lesson_id,
        )
        return 201, {"lesson_id": lesson_id, "depends_on_lesson_id": body.depends_on_lesson_id}

    return await run_idempotent(request, actor, body, handler, school_id=lesson["school_id"])


# ------------------------------------------------------------------------------------------------
# GET /v1/lessons/{id} — what Review & Publish (TR-06) shows the teacher
# ------------------------------------------------------------------------------------------------
async def current_generation(conn: AsyncConnection, lesson_id: UUID) -> dict | None:
    return await fetch_one(
        conn,
        """SELECT blueprint_id, version, generated_at FROM assessment_blueprints
            WHERE lesson_id = :l AND status = 'ACTIVE' ORDER BY version DESC LIMIT 1""",
        l=lesson_id,
    )


@router.get("/lessons/{lesson_id}")
async def get_lesson(lesson_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        lesson, _ = await load_lesson_for_teacher(conn, actor, lesson_id)
        items = await fetch_all(
            conn,
            """SELECT ci.content_item_id, ci.content_type, ci.title, ci.source_url, lc.display_order
                 FROM lesson_content lc JOIN content_items ci USING (content_item_id)
                WHERE lc.lesson_id = :l ORDER BY lc.display_order""",
            l=lesson_id,
        )
        for it in items:
            it["processing_status"] = PROCESSING_VIEW.get(await item_extraction_state(conn, it["content_item_id"]))
        gen = await current_generation(conn, lesson_id)
        generation = None
        if gen and lesson["status"] != "PROCESSING":
            concepts = await fetch_all(
                conn,
                """SELECT c.concept_id, c.name, c.description, lc.importance FROM lesson_concepts lc
                     JOIN concepts c USING (concept_id) WHERE lc.lesson_id = :l ORDER BY lc.display_order""",
                l=lesson_id,
            )
            questions = await fetch_all(
                conn,
                """SELECT question_id, concept_id, question_type, cognitive_type, prompt_text, difficulty, answer_spec
                     FROM questions WHERE blueprint_id = :b ORDER BY created_at, question_id""",
                b=gen["blueprint_id"],
            )
            generation = {
                "generation_version": gen["version"],
                "generated_at": gen["generated_at"],
                "concepts": concepts,
                "questions": questions,
            }
        assignments = await fetch_all(
            conn,
            """SELECT a.assignment_id, a.status, a.due_at, a.available_from, t.target_type, t.section_id, t.student_id
                 FROM assignments a JOIN assignment_targets t USING (assignment_id)
                WHERE a.lesson_id = :l ORDER BY a.created_at""",
            l=lesson_id,
        )
    return {
        "data": {
            "lesson_id": lesson["lesson_id"],
            "title": lesson["title"],
            "description": lesson["description"],
            "instructions": lesson["instructions"],
            "subject_id": lesson["subject_id"],
            "academic_year_id": lesson["academic_year_id"],
            "status": lesson["status"],
            "version": lesson["version"],
            "content": items,
            "generation": generation,
            "assignments": assignments,
        },
        "meta": {"request_id": request.state.request_id},
    }


# ------------------------------------------------------------------------------------------------
# POST /v1/lessons/{id}/publish — Review & Publish (§6.4)
# ------------------------------------------------------------------------------------------------
class TargetIn(BaseModel):
    type: Literal["SECTION", "STUDENT"]
    section_id: UUID | None = None
    student_id: UUID | None = None

    @model_validator(mode="after")
    def _one(self):
        if (self.type == "SECTION") != (self.section_id is not None) or (self.type == "STUDENT") != (
            self.student_id is not None
        ):
            raise ValueError("SECTION needs section_id only; STUDENT needs student_id only")
        return self


class PublishIn(BaseModel):
    version: int = Field(ge=1)
    reviewed_generation_version: int = Field(ge=1)
    target: TargetIn
    due_at: AwareDatetime
    available_from: AwareDatetime | None = None
    allow_late_submission: bool = True
    completion_mode: Literal["LEARNING_COMPLETION", "SUBMISSION_REQUIRED", "BOTH"] = "LEARNING_COMPLETION"
    title: str = Field(min_length=1, max_length=300)
    instructions: str | None = None

    @model_validator(mode="after")
    def _dates(self):
        if self.available_from and self.due_at <= self.available_from:
            raise ValueError("due_at must be after available_from")
        return self


@router.post("/lessons/{lesson_id}/publish")
async def publish_lesson(
    lesson_id: UUID,
    body: PublishIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    now = datetime.now(UTC)
    if body.due_at <= now:
        raise errors.validation_error("due_at must be in the future.", field="due_at")
    async with get_db().connect() as conn:
        lesson, teacher = await load_lesson_for_teacher(conn, actor, lesson_id)
        # Publish opens a new authorization surface: the teacher must teach this subject to the target.
        if body.target.type == "SECTION":
            section = await fetch_one(
                conn,
                """SELECT g.academic_year_id FROM sections s JOIN grades g USING (grade_id)
                    WHERE s.section_id = :s AND s.school_id = :sc""",
                s=body.target.section_id,
                sc=lesson["school_id"],
            )
            if section is None:
                raise errors.not_found("section", section_id=str(body.target.section_id))
            target_section = body.target.section_id
        else:
            enr = await fetch_one(
                conn,
                """SELECT section_id FROM student_enrollments
                    WHERE student_id = :st AND academic_year_id = :y AND status = 'ACTIVE'""",
                st=body.target.student_id,
                y=lesson["academic_year_id"],
            )
            if enr is None:
                raise errors.enrollment_inactive(student_id=str(body.target.student_id))
            target_section = enr["section_id"]
        if not await teaches_section_subject(conn, teacher.teacher_id, target_section, lesson["subject_id"]):
            raise errors.insufficient_role_scope(reason="not_teaching_target", section_id=str(target_section))
    request.state.school_id = str(lesson["school_id"])

    async def handler(conn: AsyncConnection):
        live = await fetch_one(conn, "SELECT status, version FROM lessons WHERE lesson_id = :l FOR UPDATE", l=lesson_id)
        if live["version"] != body.version:
            raise errors.version_conflict(
                entity="lessons", id=str(lesson_id), expected_version=body.version, current_version=live["version"]
            )
        # v1.3 (doc 21, Oct 5): a READY lesson is published for the first time; an already PUBLISHED
        # lesson may be published again to ANOTHER section/student (same approved generation, since
        # published content is locked).
        if live["status"] not in PUBLISHABLE:
            raise errors.lesson_not_ready(status=live["status"])
        gen = await current_generation(conn, lesson_id)
        if gen is None or gen["version"] != body.reviewed_generation_version:
            raise errors.generation_version_stale(
                reviewed_generation_version=body.reviewed_generation_version,
                current_generation_version=gen["version"] if gen else None,
            )
        existing = await fetch_one(
            conn,
            """SELECT a.assignment_id FROM assignments a JOIN assignment_targets t USING (assignment_id)
                WHERE a.lesson_id = :l AND a.status NOT IN ('CLOSED', 'ARCHIVED')
                  AND t.target_type = :tt
                  AND (t.section_id = :sec OR t.student_id = :st)""",
            l=lesson_id,
            tt=body.target.type,
            sec=body.target.section_id,
            st=body.target.student_id,
        )
        if existing:
            raise errors.AppError(
                "ALREADY_PUBLISHED_TO_TARGET",
                409,
                "This lesson is already assigned to that section/student.",
                {"assignment_id": str(existing["assignment_id"])},
            )
        first_publish = live["status"] == "READY"
        updated = await update_versioned(
            conn,
            "lessons",
            "lesson_id",
            lesson_id,
            body.version,
            {"approved_by": actor.user_id, "approved_at": now, "status": "PUBLISHED", "published_at": now}
            if first_publish
            else {},  # later publishes keep the original approval; version still bumps (double-tap guard)
        )
        scheduled = body.available_from is not None and body.available_from > now
        assignment = await fetch_one(
            conn,
            """INSERT INTO assignments (school_id, lesson_id, created_by, source_type, completion_mode, title,
                                        instructions, available_from, due_at, status, allow_late_submission,
                                        published_at)
               VALUES (:s, :l, :u, 'SCHOOL', :cm, :t, :i, :af, :due, :st, :late, :now)
               RETURNING assignment_id, status, version""",
            s=lesson["school_id"],
            l=lesson_id,
            u=actor.user_id,
            cm=body.completion_mode,
            t=body.title,
            i=body.instructions,
            af=body.available_from,
            due=body.due_at,
            st="SCHEDULED" if scheduled else "PUBLISHED",
            late=body.allow_late_submission,
            now=now,
        )
        aid = assignment["assignment_id"]
        await execute(
            conn,
            """INSERT INTO assignment_targets (assignment_id, target_type, section_id, student_id)
               VALUES (:a, :t, :sec, :st)""",
            a=aid,
            t=body.target.type,
            sec=body.target.section_id,
            st=body.target.student_id,
        )
        await execute(
            conn,
            """INSERT INTO background_jobs (school_id, job_type, entity_type, entity_id, status, max_attempts)
               VALUES (:s, 'ASSIGNMENT_FANOUT', 'assignment', :a, 'QUEUED', 5)""",
            s=lesson["school_id"],
            a=aid,
        )
        await emit(
            conn,
            ctx,
            event_type="AssignmentPublished",
            aggregate_type="assignment",
            aggregate_id=aid,
            school_id=lesson["school_id"],
            data={
                "assignment_id": aid,
                "lesson_id": lesson_id,
                "section_id": body.target.section_id,
                "student_id": body.target.student_id,
                "due_at": body.due_at,
                "completion_mode": body.completion_mode,
                "generation_version": body.reviewed_generation_version,
            },
        )
        return 200, {
            "assignment_id": aid,
            "assignment_status": assignment["status"],
            "assignment_version": assignment["version"],
            "lesson_status": updated["status"],
            "lesson_version": updated["version"],
            "distribution_status": "PENDING",
        }

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT, school_id=lesson["school_id"])


# ------------------------------------------------------------------------------------------------
# GET /v1/assignments/{id} and GET /v1/lessons/{id}/progress (TR-07)
# ------------------------------------------------------------------------------------------------
COHORT = {
    "COMPLETED": "completed",
    "SUBMITTED": "completed",
    "SUBMITTED_LATE": "completed",
    "IN_PROGRESS": "in_progress",
    "NOT_STARTED": "not_started",
    "OVERDUE": "not_started",
}


@router.get("/assignments/{assignment_id}")
async def get_assignment(assignment_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        a = await fetch_one(conn, "SELECT * FROM assignments WHERE assignment_id = :a", a=assignment_id)
        if a is None:
            raise errors.not_found("assignment", assignment_id=str(assignment_id))
        await load_lesson_for_teacher(conn, actor, a["lesson_id"])
        dist = await distribution_status(conn, assignment_id)
    return {
        "data": {
            "assignment_id": a["assignment_id"],
            "lesson_id": a["lesson_id"],
            "title": a["title"],
            "status": a["status"],
            "version": a["version"],
            "available_from": a["available_from"],
            "due_at": a["due_at"],
            "completion_mode": a["completion_mode"],
            **dist,
        },
        "meta": {"request_id": request.state.request_id},
    }


@router.get("/lessons/{lesson_id}/progress")
async def lesson_progress(lesson_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    now = datetime.now(UTC)
    async with get_db().connect() as conn:
        lesson, _ = await load_lesson_for_teacher(conn, actor, lesson_id)
        assignments = await fetch_all(
            conn, "SELECT * FROM assignments WHERE lesson_id = :l ORDER BY created_at", l=lesson_id
        )
        out = []
        for a in assignments:
            students = await fetch_all(
                conn,
                """SELECT sa.student_assignment_id, sa.student_id, u.display_name, sa.status, sa.started_at,
                          sa.completed_at, sa.is_late, coalesce(sa.effective_due_at, a.due_at) AS due_at
                     FROM student_assignments sa
                     JOIN assignments a USING (assignment_id)
                     JOIN student_profiles sp ON sp.student_id = sa.student_id
                     JOIN users u ON u.user_id = sp.user_id
                    WHERE sa.assignment_id = :a
                    ORDER BY u.display_name""",  # alphabetical: never a ranking
                a=a["assignment_id"],
            )
            cohorts: dict[str, list] = {"completed": [], "in_progress": [], "not_started": []}
            for s in students:
                s["is_overdue"] = COHORT[s["status"]] != "completed" and s["due_at"] is not None and s["due_at"] < now
                cohorts[COHORT[s["status"]]].append(s)
            out.append(
                {
                    "assignment_id": a["assignment_id"],
                    "title": a["title"],
                    "status": a["status"],
                    "due_at": a["due_at"],
                    "counts": {k: len(v) for k, v in cohorts.items()},
                    "cohorts": cohorts,
                    **(await distribution_status(conn, a["assignment_id"])),
                }
            )
    return {
        "data": {"lesson_id": lesson_id, "title": lesson["title"], "assignments": out},
        "meta": {"request_id": request.state.request_id},
    }
