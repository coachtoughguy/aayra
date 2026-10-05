"""Contract §7.4 (mastery check) and §7.6 (spaced review) — student-facing endpoints."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.auth import Actor, current_actor
from app.context import RequestContext, get_context
from app.db import fetch_all, fetch_one, get_db
from app.idempotency import OpClass, run_idempotent
from app.outbox import emit
from app.routers.learning import student_ctx
from app.services import assessment as svc

router = APIRouter(prefix="/v1", tags=["assessment"])


# ------------------------------------------------------------------------------------------------
# Mastery check
# ------------------------------------------------------------------------------------------------
class AttemptIn(BaseModel):
    lesson_id: UUID
    student_assignment_id: UUID


@router.post("/assessment-attempts", status_code=201)
async def start_attempt(
    body: AttemptIn, request: Request, actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context)
) -> JSONResponse:
    sctx = await student_ctx(actor, request)

    async def handler(conn: AsyncConnection):
        sa = await fetch_one(
            conn,
            """SELECT sa.student_assignment_id, a.lesson_id FROM student_assignments sa JOIN assignments a USING (assignment_id)
                WHERE sa.student_assignment_id = :sa AND sa.student_id = :st""",
            sa=body.student_assignment_id,
            st=sctx.student_id,
        )
        if sa is None or sa["lesson_id"] != body.lesson_id:
            raise errors.AppError("ASSIGNMENT_NOT_AVAILABLE", 404, "This assignment is not available.")
        progress = await fetch_one(
            conn, "SELECT status FROM student_lesson_progress WHERE student_assignment_id = :sa FOR UPDATE",
            sa=body.student_assignment_id,
        )  # fmt: skip
        if progress["status"] == "ASSESSING":
            active = await fetch_one(
                conn,
                """SELECT attempt_id FROM assessment_attempts WHERE student_assignment_id = :sa
                      AND review_session_id IS NULL AND status IN ('IN_PROGRESS', 'AWAITING_EVALUATION')""",
                sa=body.student_assignment_id,
            )
            raise errors.AppError("ASSESSMENT_ALREADY_ACTIVE", 409, "A check is already in progress.",
                                  {"attempt_id": str(active["attempt_id"]) if active else None})  # fmt: skip
        if progress["status"] == "COMPLETED":
            raise errors.AppError("ASSESSMENT_ALREADY_COMPLETED", 409, "This lesson's check is already complete.")
        if progress["status"] != "LEARNING_COMPLETE":
            raise errors.AppError("LESSON_NOT_FINISHED", 409, "Finish the lesson before the check.",
                                  {"status": progress["status"]})  # fmt: skip
        blueprint = await fetch_one(
            conn,
            "SELECT blueprint_id FROM assessment_blueprints WHERE lesson_id = :l AND status = 'ACTIVE' ORDER BY version DESC LIMIT 1",
            l=body.lesson_id,
        )
        if blueprint is None:
            raise errors.lesson_not_ready()
        attempt = await fetch_one(
            conn,
            """INSERT INTO assessment_attempts (student_id, lesson_id, student_assignment_id, blueprint_id, status, started_at)
               VALUES (:st, :l, :sa, :b, 'IN_PROGRESS', now()) RETURNING *""",
            st=sctx.student_id,
            l=body.lesson_id,
            sa=body.student_assignment_id,
            b=blueprint["blueprint_id"],
        )
        await fetch_one(
            conn, "UPDATE student_lesson_progress SET status = 'ASSESSING', last_activity_at = now() WHERE student_assignment_id = :sa RETURNING 1 AS x",
            sa=body.student_assignment_id,
        )  # fmt: skip
        await emit(
            conn, ctx, event_type="AssessmentStarted", aggregate_type="assessment_attempt",
            aggregate_id=attempt["attempt_id"], school_id=sctx.school_id,
            data={"student_id": sctx.student_id, "lesson_id": body.lesson_id, "blueprint_id": blueprint["blueprint_id"]},
        )  # fmt: skip
        nxt = await svc.advance(conn, ctx, attempt["attempt_id"])
        return 201, {"attempt_id": attempt["attempt_id"], "status": "IN_PROGRESS", "next": nxt}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.EVIDENCE, school_id=sctx.school_id)


async def _attempt_view(conn: AsyncConnection, attempt: dict[str, Any]) -> dict[str, Any]:
    responses = await fetch_all(
        conn,
        """SELECT r.response_id, r.question_id, r.response_sequence, re.evaluation, re.help_level
             FROM assessment_responses r
             LEFT JOIN response_evaluations re ON re.response_id = r.response_id AND re.is_current
            WHERE r.assessment_attempt_id = :a ORDER BY r.response_sequence""",
        a=attempt["attempt_id"],
    )
    current = await svc.open_question(conn, attempt["attempt_id"])
    view = {
        "attempt_id": attempt["attempt_id"],
        "lesson_id": attempt["lesson_id"],
        "status": attempt["status"],
        "review_session_id": attempt["review_session_id"],
        "responses": [
            {**r, "evaluation_status": "PENDING" if r["evaluation"] is None else "EVALUATED"} for r in responses
        ],
        "current_question": ({"mode": current[1], **svc.student_view(current[0])} if current else None),
    }
    if attempt["status"] == "COMPLETED":
        view["result"] = await svc.attempt_result(conn, attempt["attempt_id"])
    return view


@router.get("/assessment-attempts/{attempt_id}")
async def get_attempt(attempt_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    sctx = await student_ctx(actor, request)
    async with get_db().connect() as conn:
        attempt = await svc.load_attempt(conn, attempt_id)
        if attempt["student_id"] != sctx.student_id:
            raise errors.not_found("assessment_attempt", attempt_id=str(attempt_id))
        view = await _attempt_view(conn, attempt)
    return {"data": view, "meta": {"request_id": request.state.request_id}}


class ResponseIn(BaseModel):
    question_id: UUID
    answer: dict[str, Any]


async def _submit(
    request: Request, actor: Actor, ctx: RequestContext, attempt_id: UUID, body: ResponseIn
) -> JSONResponse:
    sctx = await student_ctx(actor, request)

    async def handler(conn: AsyncConnection):
        return await svc.submit_response(conn, ctx, attempt_id, sctx.student_id, body.question_id, body.answer)

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.EVIDENCE, school_id=sctx.school_id)


@router.post("/assessment-attempts/{attempt_id}/responses")
async def submit_attempt_response(
    attempt_id: UUID,
    body: ResponseIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    return await _submit(request, actor, ctx, attempt_id, body)


# ------------------------------------------------------------------------------------------------
# Spaced review (§7.6)
# ------------------------------------------------------------------------------------------------
async def review_items(conn: AsyncConnection, student_id: UUID, tz: ZoneInfo) -> list[dict[str, Any]]:
    today = datetime.now(UTC).astimezone(tz).date()
    rows = await fetch_all(
        conn,
        """SELECT i.review_schedule_item_id, i.review_day, i.scheduled_for, rs.lesson_id, l.title AS lesson_title,
                  rs.concept_id, c.name AS concept_name, sub.name AS subject_name, sub.subject_id
             FROM review_schedule_items i
             JOIN review_schedules rs ON rs.review_schedule_id = i.review_schedule_id
             JOIN lessons l ON l.lesson_id = rs.lesson_id
             JOIN concepts c ON c.concept_id = rs.concept_id
             JOIN subjects sub ON sub.subject_id = l.subject_id
            WHERE rs.student_id = :st AND rs.status = 'ACTIVE' AND i.status IN ('UPCOMING', 'DUE', 'OVERDUE')
            ORDER BY i.scheduled_for, l.title, c.name""",
        st=student_id,
    )
    for r in rows:
        r["state"] = "OVERDUE" if r["scheduled_for"] < today else ("DUE" if r["scheduled_for"] == today else "UPCOMING")
    return rows


@router.get("/students/me/reviews")
async def my_reviews(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    sctx = await student_ctx(actor, request)
    async with get_db().connect() as conn:
        school = await fetch_one(conn, "SELECT timezone FROM schools WHERE school_id = :s", s=sctx.school_id)
        items = await review_items(conn, sctx.student_id, ZoneInfo(school["timezone"]))
    return {
        "data": {
            "due": [i for i in items if i["state"] != "UPCOMING"],
            "upcoming": [i for i in items if i["state"] == "UPCOMING"],
        },
        "meta": {"request_id": request.state.request_id},
    }


class ReviewSessionIn(BaseModel):
    review_schedule_item_id: UUID


@router.post("/review-sessions", status_code=201)
async def start_review(
    body: ReviewSessionIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    sctx = await student_ctx(actor, request)

    async def handler(conn: AsyncConnection):
        item = await fetch_one(
            conn,
            """SELECT i.*, rs.student_id, rs.lesson_id, rs.concept_id, s.timezone
                 FROM review_schedule_items i
                 JOIN review_schedules rs ON rs.review_schedule_id = i.review_schedule_id
                 JOIN lessons l ON l.lesson_id = rs.lesson_id JOIN schools s ON s.school_id = l.school_id
                WHERE i.review_schedule_item_id = :i FOR UPDATE OF i""",
            i=body.review_schedule_item_id,
        )
        if item is None or item["student_id"] != sctx.student_id:
            raise errors.not_found("review", review_schedule_item_id=str(body.review_schedule_item_id))
        if item["status"] == "COMPLETED":
            raise errors.AppError("REVIEW_ALREADY_COMPLETED", 409, "This review is already done.")
        today = datetime.now(UTC).astimezone(ZoneInfo(item["timezone"])).date()
        if item["scheduled_for"] > today:
            raise errors.AppError("REVIEW_NOT_DUE", 409, "This review isn't due yet.",
                                  {"scheduled_for": item["scheduled_for"].isoformat()})  # fmt: skip
        existing = await fetch_one(
            conn,
            """SELECT s.review_session_id, a.attempt_id FROM review_sessions s
                 JOIN assessment_attempts a ON a.review_session_id = s.review_session_id
                WHERE s.review_schedule_item_id = :i AND s.status = 'IN_PROGRESS'""",
            i=body.review_schedule_item_id,
        )
        if existing:  # resume rather than open a second session for the same review
            nxt = await svc.advance(conn, ctx, existing["attempt_id"])
            return 200, {**existing, "next": nxt, "resumed": True}
        blueprint = await fetch_one(
            conn,
            "SELECT blueprint_id FROM assessment_blueprints WHERE lesson_id = :l AND status = 'ACTIVE' ORDER BY version DESC LIMIT 1",
            l=item["lesson_id"],
        )
        sa = await fetch_one(
            conn,
            """SELECT sa.student_assignment_id FROM student_assignments sa JOIN assignments a USING (assignment_id)
                WHERE sa.student_id = :st AND a.lesson_id = :l ORDER BY sa.completed_at DESC NULLS LAST LIMIT 1""",
            st=sctx.student_id,
            l=item["lesson_id"],
        )
        session = await fetch_one(
            conn,
            """INSERT INTO review_sessions (student_id, review_schedule_item_id, started_at, status)
               VALUES (:st, :i, now(), 'IN_PROGRESS') RETURNING review_session_id""",
            st=sctx.student_id,
            i=body.review_schedule_item_id,
        )
        attempt = await fetch_one(
            conn,
            """INSERT INTO assessment_attempts (student_id, lesson_id, student_assignment_id, blueprint_id, status,
                                                started_at, review_session_id)
               VALUES (:st, :l, :sa, :b, 'IN_PROGRESS', now(), :rs) RETURNING attempt_id""",
            st=sctx.student_id,
            l=item["lesson_id"],
            sa=sa["student_assignment_id"] if sa else None,
            b=blueprint["blueprint_id"],
            rs=session["review_session_id"],
        )
        nxt = await svc.advance(conn, ctx, attempt["attempt_id"])
        return 201, {"review_session_id": session["review_session_id"], "attempt_id": attempt["attempt_id"],
                     "review_day": item["review_day"], "next": nxt}  # fmt: skip

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.EVIDENCE, school_id=sctx.school_id)


@router.post("/review-sessions/{review_session_id}/responses")
async def submit_review_response(
    review_session_id: UUID,
    body: ResponseIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    """Same machinery as the mastery check (contract §7.6): delegates to the companion attempt."""
    async with get_db().connect() as conn:
        row = await fetch_one(
            conn, "SELECT attempt_id FROM assessment_attempts WHERE review_session_id = :s", s=review_session_id
        )
    if row is None:
        raise errors.not_found("review_session", review_session_id=str(review_session_id))
    return await _submit(request, actor, ctx, row["attempt_id"], body)
