"""Parent-assigned and self-chosen learning (contract §9 inventory; doc 21 v0.9 PA-05..PA-07; Oct 5 decision).

Flow (same for parents and students, lightweight compared to a teacher's Review & Publish):
  1. upload content (POST /v1/content-items with purpose PARENT or SELF),
  2. create:  POST /v1/parent-assignments | /v1/self-assignments  -> lesson is prepared by the same AI
     pipeline, the assignment waits in DRAFT,
  3. check:   GET .../{id}  (status PREPARING -> READY_TO_ASSIGN, light preview of what was understood),
  4. assign:  POST .../{id}/assign  -> delivered to the student (fan-out + notification).

Rules:
- The creator chooses review_mode SPACED or ONE_TIME. Mastery is scoped to PARENT / SELF and never
  touches school mastery (enforced in MasteryEngine).
- No dependency/sequencing configuration and no teacher-grade editing (doc 21 v0.9).
- Parent/self lessons and assignments are private: teachers can't open them (services/teaching.py).
- Only guardians with an ACTIVE link (and the PARENT_GUARDIAN role in the child's school) can assign.
- Self-learning here is for enrolled students (13+, inside their school/year). Unaffiliated 18+
  self-learners need a school-less tenancy model that does not exist in the schema yet.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.auth import Actor, current_actor
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one, get_db
from app.idempotency import OpClass, run_idempotent
from app.outbox import emit
from app.routers.learning import student_ctx
from app.services.pipeline import enqueue_lesson_preparation, item_extraction_state

router = APIRouter(prefix="/v1", tags=["family-learning"])

Source = Literal["PARENT", "SELF"]


# ------------------------------------------------------------------------------------------------
# Guardian access
# ------------------------------------------------------------------------------------------------
async def child_context(conn: AsyncConnection, student_id: UUID) -> dict:
    row = await fetch_one(
        conn,
        """SELECT se.student_id, se.school_id, se.section_id, se.grade_id, se.academic_year_id, sp.user_id,
                  u.display_name
             FROM student_enrollments se
             JOIN academic_years ay ON ay.academic_year_id = se.academic_year_id AND ay.status = 'ACTIVE'
             JOIN student_profiles sp ON sp.student_id = se.student_id AND sp.status = 'ACTIVE'
             JOIN users u ON u.user_id = sp.user_id
            WHERE se.student_id = :s AND se.status = 'ACTIVE' ORDER BY ay.start_date DESC LIMIT 1""",
        s=student_id,
    )
    if row is None:
        raise errors.enrollment_inactive(student_id=str(student_id))
    return row


async def require_guardian_of(conn: AsyncConnection, actor: Actor, student_id: UUID) -> dict:
    link = await fetch_one(
        conn,
        """SELECT sg.student_guardian_id FROM student_guardians sg JOIN guardian_profiles gp USING (guardian_id)
            WHERE gp.user_id = :u AND sg.student_id = :s AND sg.status = 'ACTIVE'""",
        u=actor.user_id,
        s=student_id,
    )
    if link is None:
        raise errors.not_found("student", student_id=str(student_id))  # don't reveal other families' children
    child = await child_context(conn, student_id)
    actor.require_role(child["school_id"], "PARENT_GUARDIAN")
    return child


@router.get("/guardians/me/children")
async def my_children(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        rows = await fetch_all(
            conn,
            """SELECT sp.student_id, u.display_name, sg.relationship_type, sg.guardian_priority, s.name AS school_name,
                      sec.name AS section_name
                 FROM guardian_profiles gp
                 JOIN student_guardians sg ON sg.guardian_id = gp.guardian_id AND sg.status = 'ACTIVE'
                 JOIN student_profiles sp ON sp.student_id = sg.student_id
                 JOIN users u ON u.user_id = sp.user_id
                 JOIN schools s ON s.school_id = sg.school_id
                 LEFT JOIN student_enrollments se ON se.student_id = sp.student_id AND se.status = 'ACTIVE'
                 LEFT JOIN sections sec ON sec.section_id = se.section_id
                WHERE gp.user_id = :u ORDER BY u.display_name""",
            u=actor.user_id,
        )
    return {"data": {"children": rows}, "meta": {"request_id": request.state.request_id}}


# ------------------------------------------------------------------------------------------------
# Create
# ------------------------------------------------------------------------------------------------
class FamilyAssignmentIn(BaseModel):
    subject_id: UUID
    title: str = Field(min_length=1, max_length=300)
    instructions: str | None = None
    content_item_ids: list[UUID] = Field(min_length=1, max_length=10)
    due_at: AwareDatetime | None = None
    review_mode: Literal["SPACED", "ONE_TIME"]


class ParentAssignmentIn(FamilyAssignmentIn):
    student_id: UUID


async def _create(
    conn: AsyncConnection, ctx: RequestContext, actor: Actor, child: dict, body: FamilyAssignmentIn, source: Source
) -> dict:
    if body.due_at and body.due_at <= datetime.now(UTC):
        raise errors.validation_error("due_at must be in the future.", field="due_at")
    offered = await fetch_one(
        conn,
        "SELECT 1 AS x FROM grade_subjects WHERE grade_id = :g AND subject_id = :s",
        g=child["grade_id"],
        s=body.subject_id,
    )
    if offered is None:
        raise errors.validation_error("Choose one of the student's subjects.", field="subject_id")
    item_ids = list(dict.fromkeys(body.content_item_ids))
    items = await fetch_all(
        conn,
        """SELECT content_item_id, object_key, source_url FROM content_items
            WHERE content_item_id = ANY(:ids) AND uploaded_by = :u AND school_id = :s""",
        ids=item_ids,
        u=actor.user_id,
        s=child["school_id"],
    )
    if len(items) != len(item_ids):
        raise errors.not_found("content_item", detail="Use content you uploaded for this student.")
    if any(not (i["object_key"] or i["source_url"]) for i in items):
        raise errors.AppError("CONTENT_NOT_UPLOADED", 409, "Finish uploading every file first.")

    lesson = await fetch_one(
        conn,
        """INSERT INTO lessons (school_id, academic_year_id, subject_id, created_by, title, instructions, status,
                                requires_teacher_approval)
           VALUES (:s, :y, :sub, :u, :t, :i, 'PROCESSING', false) RETURNING lesson_id""",
        s=child["school_id"],
        y=child["academic_year_id"],
        sub=body.subject_id,
        u=actor.user_id,
        t=body.title,
        i=body.instructions,
    )
    lesson_id = lesson["lesson_id"]
    for order, item_id in enumerate(item_ids, start=1):
        await execute(
            conn, "INSERT INTO lesson_content (lesson_id, content_item_id, display_order) VALUES (:l, :c, :o)",
            l=lesson_id, c=item_id, o=order,
        )  # fmt: skip
        if await item_extraction_state(conn, item_id) in ("SUCCEEDED", "FAILED"):
            await enqueue_lesson_preparation(conn, child["school_id"], lesson_id)
    assignment = await fetch_one(
        conn,
        """INSERT INTO assignments (school_id, lesson_id, created_by, source_type, completion_mode, title, instructions,
                                    due_at, status, allow_late_submission, review_mode)
           VALUES (:s, :l, :u, :src, 'LEARNING_COMPLETION', :t, :i, :due, 'DRAFT', true, :rm)
           RETURNING assignment_id, version""",
        s=child["school_id"],
        l=lesson_id,
        u=actor.user_id,
        src=source,
        t=body.title,
        i=body.instructions,
        due=body.due_at,
        rm=body.review_mode,
    )
    await execute(
        conn, "INSERT INTO assignment_targets (assignment_id, target_type, student_id) VALUES (:a, 'STUDENT', :s)",
        a=assignment["assignment_id"], s=child["student_id"],
    )  # fmt: skip
    await emit(
        conn, ctx, event_type=f"{source.title()}AssignmentCreated", aggregate_type="assignment",
        aggregate_id=assignment["assignment_id"], school_id=child["school_id"],
        data={"lesson_id": lesson_id, "student_id": child["student_id"], "review_mode": body.review_mode},
    )  # fmt: skip
    return {"assignment_id": assignment["assignment_id"], "lesson_id": lesson_id, "status": "PREPARING",
            "version": assignment["version"], "review_mode": body.review_mode}  # fmt: skip


@router.post("/parent-assignments", status_code=201)
async def create_parent_assignment(
    body: ParentAssignmentIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        child = await require_guardian_of(conn, actor, body.student_id)
    request.state.school_id = str(child["school_id"])

    async def handler(conn: AsyncConnection):
        return 201, await _create(conn, ctx, actor, child, body, "PARENT")

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT, school_id=child["school_id"])


@router.post("/self-assignments", status_code=201)
async def create_self_assignment(
    body: FamilyAssignmentIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    sctx = await student_ctx(actor, request)
    async with get_db().connect() as conn:
        child = await child_context(conn, sctx.student_id)

    async def handler(conn: AsyncConnection):
        return 201, await _create(conn, ctx, actor, child, body, "SELF")

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT, school_id=child["school_id"])


# ------------------------------------------------------------------------------------------------
# Check + assign
# ------------------------------------------------------------------------------------------------
async def _own_assignment(
    conn: AsyncConnection, actor: Actor, assignment_id: UUID, source: Source, *, lock: bool = False
) -> dict:
    row = await fetch_one(
        conn,
        f"""SELECT a.*, l.status AS lesson_status, l.version AS lesson_version, t.student_id
              FROM assignments a JOIN lessons l USING (lesson_id) JOIN assignment_targets t USING (assignment_id)
             WHERE a.assignment_id = :a AND a.source_type = :src AND a.created_by = :u{" FOR UPDATE OF a" if lock else ""}""",
        a=assignment_id,
        src=source,
        u=actor.user_id,
    )
    if row is None:
        raise errors.not_found("assignment", assignment_id=str(assignment_id))
    if source == "PARENT":
        await require_guardian_of(conn, actor, row["student_id"])  # link may have been revoked since
    return row


def _state(row: dict) -> str:
    if row["status"] != "DRAFT":
        return "ASSIGNED"
    return {"READY": "READY_TO_ASSIGN", "PROCESSING_FAILED": "FAILED"}.get(row["lesson_status"], "PREPARING")


async def _view(conn: AsyncConnection, row: dict) -> dict:
    content = await fetch_all(
        conn,
        """SELECT ci.content_item_id, ci.title, ci.content_type FROM lesson_content lc JOIN content_items ci USING (content_item_id)
            WHERE lc.lesson_id = :l ORDER BY lc.display_order""",
        l=row["lesson_id"],
    )
    for c in content:
        c["processing_status"] = await item_extraction_state(conn, c["content_item_id"]) or "QUEUED"
    preview = None
    if row["lesson_status"] in ("READY", "PUBLISHED", "ACTIVE"):
        concepts = await fetch_all(
            conn,
            """SELECT c.name FROM lesson_concepts lc JOIN concepts c USING (concept_id)
                WHERE lc.lesson_id = :l ORDER BY lc.display_order""",
            l=row["lesson_id"],
        )
        questions = await fetch_one(
            conn,
            """SELECT count(*) AS n FROM questions q JOIN assessment_blueprints b ON b.blueprint_id = q.blueprint_id
                WHERE b.lesson_id = :l AND b.status = 'ACTIVE'""",
            l=row["lesson_id"],
        )
        preview = {"topics": [c["name"] for c in concepts], "check_questions": questions["n"]}
    return {
        "assignment_id": row["assignment_id"],
        "lesson_id": row["lesson_id"],
        "student_id": row["student_id"],
        "title": row["title"],
        "status": _state(row),
        "version": row["version"],
        "review_mode": row["review_mode"],
        "due_at": row["due_at"],
        "content": content,
        "preview": preview,
    }


async def _get(request: Request, actor: Actor, assignment_id: UUID, source: Source) -> dict:
    async with get_db().connect() as conn:
        row = await _own_assignment(conn, actor, assignment_id, source)
        view = await _view(conn, row)
    return {"data": view, "meta": {"request_id": request.state.request_id}}


@router.get("/parent-assignments/{assignment_id}")
async def get_parent_assignment(assignment_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    return await _get(request, actor, assignment_id, "PARENT")


@router.get("/self-assignments/{assignment_id}")
async def get_self_assignment(assignment_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    return await _get(request, actor, assignment_id, "SELF")


class AssignIn(BaseModel):
    version: int = Field(ge=1)


async def _assign(
    request: Request, actor: Actor, ctx: RequestContext, assignment_id: UUID, body: AssignIn, source: Source
):
    async def handler(conn: AsyncConnection):
        row = await _own_assignment(conn, actor, assignment_id, source, lock=True)
        if row["version"] != body.version:
            raise errors.version_conflict(entity="assignments", id=str(assignment_id), expected_version=body.version,
                                          current_version=row["version"])  # fmt: skip
        if row["status"] != "DRAFT":
            raise errors.AppError("ALREADY_ASSIGNED", 409, "This was already assigned.")
        if row["lesson_status"] != "READY":
            raise errors.lesson_not_ready(status=row["lesson_status"])
        if row["due_at"] and row["due_at"] <= datetime.now(UTC):
            raise errors.validation_error("The due date has passed; create it again with a new due date.")
        await execute(
            conn,
            """UPDATE lessons SET status = 'PUBLISHED', approved_by = :u, approved_at = now(), published_at = now(),
                      version = version + 1 WHERE lesson_id = :l""",
            u=actor.user_id,
            l=row["lesson_id"],
        )
        updated = await fetch_one(
            conn,
            """UPDATE assignments SET status = 'PUBLISHED', published_at = now(), version = version + 1
                WHERE assignment_id = :a RETURNING version""",
            a=assignment_id,
        )
        await execute(
            conn,
            """INSERT INTO background_jobs (school_id, job_type, entity_type, entity_id, status, max_attempts)
               VALUES (:s, 'ASSIGNMENT_FANOUT', 'assignment', :a, 'QUEUED', 5)""",
            s=row["school_id"],
            a=assignment_id,
        )
        await emit(
            conn, ctx, event_type="AssignmentPublished", aggregate_type="assignment", aggregate_id=assignment_id,
            school_id=row["school_id"],
            data={"assignment_id": assignment_id, "lesson_id": row["lesson_id"], "student_id": row["student_id"],
                  "source": source, "review_mode": row["review_mode"], "due_at": row["due_at"]},
        )  # fmt: skip
        return 200, {"assignment_id": assignment_id, "status": "ASSIGNED", "version": updated["version"]}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT)


@router.post("/parent-assignments/{assignment_id}/assign")
async def assign_parent(
    assignment_id: UUID, body: AssignIn, request: Request, actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    return await _assign(request, actor, ctx, assignment_id, body, "PARENT")


@router.post("/self-assignments/{assignment_id}/assign")
async def assign_self(
    assignment_id: UUID, body: AssignIn, request: Request, actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    await student_ctx(actor, request)
    return await _assign(request, actor, ctx, assignment_id, body, "SELF")
