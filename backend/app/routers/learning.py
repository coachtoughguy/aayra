"""Contract §7.1–7.3: start an assignment, focus cycles (server-derived duration), finish the lesson.

Lesson material is one continuous item with a focus timer (doc 21, Sept 15/16): the timer suggests
breaks but never implies completion; only Finish Lesson leads to the mastery check.
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.auth import Actor, StudentContext, current_actor, resolve_student_context
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one, get_db
from app.idempotency import OpClass, run_idempotent
from app.outbox import emit

router = APIRouter(prefix="/v1", tags=["learning"])

MAX_FOCUS_SECONDS = 4 * 3600  # longer than this is a forgotten-open session or clock drift


async def student_ctx(actor: Actor, request: Request) -> StudentContext:
    async with get_db().connect() as conn:
        ctx = await resolve_student_context(conn, actor)
    request.state.school_id = str(ctx.school_id)
    return ctx


async def load_student_assignment(conn: AsyncConnection, student_id: UUID, sa_id: UUID, *, lock: bool = False) -> dict:
    row = await fetch_one(
        conn,
        f"""SELECT sa.*, a.lesson_id, a.status AS assignment_status, a.available_from, a.school_id AS a_school
              FROM student_assignments sa JOIN assignments a USING (assignment_id)
             WHERE sa.student_assignment_id = :sa AND sa.student_id = :st
               AND a.status IN ('PUBLISHED', 'ACTIVE', 'CLOSED')
               AND (a.available_from IS NULL OR a.available_from <= now()){" FOR UPDATE OF sa" if lock else ""}""",
        sa=sa_id,
        st=student_id,
    )
    if row is None:
        raise errors.AppError("ASSIGNMENT_NOT_AVAILABLE", 404, "This assignment is not available.")
    return row


async def resolve_for_lesson(conn: AsyncConnection, student_id: UUID, lesson_id: UUID, sa_id: UUID | None) -> dict:
    if sa_id is None:
        row = await fetch_one(
            conn,
            """SELECT sa.student_assignment_id FROM student_assignments sa JOIN assignments a USING (assignment_id)
                WHERE sa.student_id = :st AND a.lesson_id = :l AND sa.status NOT IN ('COMPLETED', 'SUBMITTED_LATE')
                ORDER BY sa.created_at DESC LIMIT 1""",
            st=student_id,
            l=lesson_id,
        )
        if row is None:
            raise errors.AppError("ASSIGNMENT_NOT_AVAILABLE", 404, "No open assignment for this lesson.")
        sa_id = row["student_assignment_id"]
    sa = await load_student_assignment(conn, student_id, sa_id)
    if sa["lesson_id"] != lesson_id:
        raise errors.validation_error("student_assignment_id does not belong to this lesson.")
    return sa


# ------------------------------------------------------------------------------------------------
# §7.1 start
# ------------------------------------------------------------------------------------------------
@router.post("/student-assignments/{student_assignment_id}/start")
async def start_assignment(
    student_assignment_id: UUID,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    sctx = await student_ctx(actor, request)

    async def handler(conn: AsyncConnection):
        sa = await load_student_assignment(conn, sctx.student_id, student_assignment_id, lock=True)
        # Dependency is the only sequencing mechanism (doc 21 v0.7): unmet prerequisite = locked.
        blocked = await fetch_all(
            conn,
            """SELECT d.depends_on_lesson_id, l.title FROM lesson_dependencies d JOIN lessons l
                    ON l.lesson_id = d.depends_on_lesson_id
                WHERE d.lesson_id = :l AND NOT EXISTS (
                  SELECT 1 FROM student_lesson_progress p
                   WHERE p.student_id = :st AND p.lesson_id = d.depends_on_lesson_id AND p.status = 'COMPLETED')""",
            l=sa["lesson_id"],
            st=sctx.student_id,
        )
        if blocked:
            raise errors.AppError(
                "PREREQUISITE_NOT_COMPLETED",
                409,
                "Finish the prerequisite lessons first.",
                {
                    "prerequisites": [
                        {"lesson_id": str(b["depends_on_lesson_id"]), "title": b["title"]} for b in blocked
                    ]
                },
            )
        if sa["status"] in ("NOT_STARTED", "OVERDUE"):
            sa = await fetch_one(
                conn,
                """UPDATE student_assignments SET status = 'IN_PROGRESS', started_at = now(), version = version + 1
                    WHERE student_assignment_id = :sa RETURNING *""",
                sa=student_assignment_id,
            )
            await execute(
                conn,
                """UPDATE student_lesson_progress SET status = 'IN_PROGRESS', first_started_at = coalesce(first_started_at, now()),
                          last_activity_at = now()
                    WHERE student_assignment_id = :sa AND status = 'NOT_STARTED'""",
                sa=student_assignment_id,
            )
            await emit(
                conn,
                ctx,
                event_type="StudentAssignmentStarted",
                aggregate_type="student_assignment",
                aggregate_id=student_assignment_id,
                school_id=sctx.school_id,
                data={"student_id": sctx.student_id, "student_assignment_id": student_assignment_id},
            )
        return 200, {"student_assignment_id": student_assignment_id, "status": sa["status"], "version": sa["version"]}

    return await run_idempotent(
        request, actor, {"start": str(student_assignment_id)}, handler, school_id=sctx.school_id
    )


# ------------------------------------------------------------------------------------------------
# §7.2 focus cycles
# ------------------------------------------------------------------------------------------------
class FocusStartIn(BaseModel):
    student_assignment_id: UUID | None = None
    planned_minutes: int | None = Field(default=None, ge=1, le=180)


@router.post("/lessons/{lesson_id}/focus-cycles", status_code=201)
async def start_focus_cycle(
    lesson_id: UUID,
    body: FocusStartIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    sctx = await student_ctx(actor, request)

    async def handler(conn: AsyncConnection):
        sa = await resolve_for_lesson(conn, sctx.student_id, lesson_id, body.student_assignment_id)
        if sa["status"] not in ("IN_PROGRESS",):
            raise errors.AppError(
                "ASSIGNMENT_NOT_STARTED", 409, "Start the assignment first.", {"status": sa["status"]}
            )
        await fetch_one(conn, "SELECT 1 AS x FROM student_profiles WHERE student_id = :s FOR UPDATE", s=sctx.student_id)
        active = await fetch_one(
            conn,
            "SELECT focus_cycle_id FROM focus_cycles WHERE student_id = :s AND ended_at IS NULL",
            s=sctx.student_id,
        )
        if active:
            raise errors.AppError(
                "FOCUS_CYCLE_ALREADY_ACTIVE", 409, "A focus session is already running.",
                {"focus_cycle_id": str(active["focus_cycle_id"])},
            )  # fmt: skip
        row = await fetch_one(
            conn,
            """INSERT INTO focus_cycles (student_id, lesson_id, student_assignment_id, started_at, planned_minutes)
               VALUES (:st, :l, :sa, now(), :pm) RETURNING focus_cycle_id, started_at, planned_minutes""",
            st=sctx.student_id,
            l=lesson_id,
            sa=sa["student_assignment_id"],
            pm=body.planned_minutes,
        )
        await execute(
            conn, "UPDATE student_lesson_progress SET last_activity_at = now() WHERE student_assignment_id = :sa",
            sa=sa["student_assignment_id"],
        )  # fmt: skip
        return 201, {**row, "lesson_id": lesson_id, "student_assignment_id": sa["student_assignment_id"]}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.EVIDENCE, school_id=sctx.school_id)


class FocusEndIn(BaseModel):
    end_reason: Literal["BREAK", "CONTINUE_LATER", "INTERRUPTED"]
    client_reported_seconds: int | None = Field(default=None, ge=0, le=86_400)


async def _close_cycle(
    conn: AsyncConnection, ctx: RequestContext, school_id: UUID, cycle_id: UUID, reason: str
) -> dict:
    row = await fetch_one(
        conn,
        """UPDATE focus_cycles SET ended_at = now(), end_reason = :r,
                  actual_seconds = LEAST(:cap, GREATEST(0, extract(epoch FROM now() - started_at)))::int
            WHERE focus_cycle_id = :c AND ended_at IS NULL
            RETURNING focus_cycle_id, student_assignment_id, actual_seconds, end_reason, ended_at""",
        r=reason,
        cap=MAX_FOCUS_SECONDS,
        c=cycle_id,
    )
    await execute(
        conn,
        """UPDATE student_lesson_progress SET total_active_seconds = total_active_seconds + :s, last_activity_at = now()
            WHERE student_assignment_id = :sa""",
        s=row["actual_seconds"],
        sa=row["student_assignment_id"],
    )
    await emit(
        conn,
        ctx,
        event_type="FocusCycleEnded",
        aggregate_type="focus_cycle",
        aggregate_id=cycle_id,
        school_id=school_id,
        data={"end_reason": reason, "actual_seconds": row["actual_seconds"]},
    )
    return row


@router.post("/focus-cycles/{focus_cycle_id}/end")
async def end_focus_cycle(
    focus_cycle_id: UUID,
    body: FocusEndIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    sctx = await student_ctx(actor, request)

    async def handler(conn: AsyncConnection):
        cycle = await fetch_one(
            conn, "SELECT * FROM focus_cycles WHERE focus_cycle_id = :c AND student_id = :s FOR UPDATE",
            c=focus_cycle_id, s=sctx.student_id,
        )  # fmt: skip
        if cycle is None:
            raise errors.not_found("focus_cycle", focus_cycle_id=str(focus_cycle_id))
        if cycle["ended_at"] is not None:
            raise errors.AppError("FOCUS_CYCLE_ALREADY_ENDED", 409, "This focus session already ended.")
        # client_reported_seconds is telemetry only (clock-skew diagnostics); never authoritative.
        await execute(
            conn, "UPDATE focus_cycles SET client_reported_seconds = :s WHERE focus_cycle_id = :c",
            s=body.client_reported_seconds, c=focus_cycle_id,
        )  # fmt: skip
        return 200, await _close_cycle(conn, ctx, sctx.school_id, focus_cycle_id, body.end_reason)

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.EVIDENCE, school_id=sctx.school_id)


# ------------------------------------------------------------------------------------------------
# §7.3 finish
# ------------------------------------------------------------------------------------------------
class FinishIn(BaseModel):
    student_assignment_id: UUID | None = None


@router.post("/lessons/{lesson_id}/finish")
async def finish_lesson(
    lesson_id: UUID,
    body: FinishIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    sctx = await student_ctx(actor, request)

    async def handler(conn: AsyncConnection):
        sa = await resolve_for_lesson(conn, sctx.student_id, lesson_id, body.student_assignment_id)
        progress = await fetch_one(
            conn, "SELECT * FROM student_lesson_progress WHERE student_assignment_id = :sa FOR UPDATE",
            sa=sa["student_assignment_id"],
        )  # fmt: skip
        if progress["status"] != "IN_PROGRESS":
            raise errors.AppError("LESSON_NOT_IN_PROGRESS", 409, "Only a lesson in progress can be finished.",
                                  {"status": progress["status"]})  # fmt: skip
        open_cycle = await fetch_one(
            conn, "SELECT focus_cycle_id FROM focus_cycles WHERE student_id = :s AND ended_at IS NULL",
            s=sctx.student_id,
        )  # fmt: skip
        if open_cycle:
            await _close_cycle(conn, ctx, sctx.school_id, open_cycle["focus_cycle_id"], "FINISH_LESSON")
        row = await fetch_one(
            conn,
            """UPDATE student_lesson_progress SET status = 'LEARNING_COMPLETE', finished_learning_at = now(),
                      last_activity_at = now(),
                      total_active_seconds = (SELECT coalesce(sum(actual_seconds), 0) FROM focus_cycles
                                               WHERE student_assignment_id = :sa)
                WHERE student_assignment_id = :sa RETURNING status, total_active_seconds, finished_learning_at""",
            sa=sa["student_assignment_id"],
        )
        await emit(
            conn,
            ctx,
            event_type="LessonLearningCompleted",
            aggregate_type="student_assignment",
            aggregate_id=sa["student_assignment_id"],
            school_id=sctx.school_id,
            data={
                "student_id": sctx.student_id,
                "lesson_id": lesson_id,
                "total_active_seconds": row["total_active_seconds"],
            },
        )
        return 200, {"lesson_id": lesson_id, "student_assignment_id": sa["student_assignment_id"], **row}

    return await run_idempotent(request, actor, body, handler, school_id=sctx.school_id)
