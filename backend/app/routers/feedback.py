"""Teacher feedback (doc 21 Sept 9: optional, may be AI-drafted, REQUIRES teacher approval before
sending, triggers a student notification/deep link; contract §7.5: visible_to_student flips only at
publish).

The AI only ever writes `ai_draft_text`. Nothing reaches a student until the teacher publishes, and
what is published is the teacher's `feedback_text`.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.ai.grading_ai import get_grading_ai
from app.auth import Actor, current_actor
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one, get_db
from app.idempotency import run_idempotent
from app.outbox import emit
from app.routers.learning import load_student_assignment, student_ctx
from app.services.teaching import load_lesson_for_teacher

router = APIRouter(prefix="/v1", tags=["feedback"])


async def _context(conn: AsyncConnection, sa_id: UUID) -> dict:
    row = await fetch_one(
        conn,
        """SELECT sa.student_assignment_id, sa.student_id, sa.assignment_id, a.lesson_id, a.title, a.school_id,
                  u.display_name, sp.user_id AS student_user_id
             FROM student_assignments sa JOIN assignments a USING (assignment_id)
             JOIN student_profiles sp ON sp.student_id = sa.student_id JOIN users u ON u.user_id = sp.user_id
            WHERE sa.student_assignment_id = :sa""",
        sa=sa_id,
    )
    if row is None:
        raise errors.not_found("student_assignment", student_assignment_id=str(sa_id))
    return row


async def _teacher_for(conn: AsyncConnection, actor: Actor, sa_id: UUID) -> tuple[dict, UUID]:
    ctx = await _context(conn, sa_id)
    _, teacher = await load_lesson_for_teacher(conn, actor, ctx["lesson_id"])
    return ctx, teacher.teacher_id


class FeedbackIn(BaseModel):
    student_assignment_id: UUID
    submission_id: UUID | None = None
    feedback_text: str | None = Field(default=None, max_length=5000)
    ai_draft: bool = False


def _view(row: dict) -> dict:
    return {k: row[k] for k in ("feedback_id", "student_id", "assignment_id", "submission_id", "feedback_text",
                                "ai_draft_text", "status", "visible_to_student", "published_at")}  # fmt: skip


@router.post("/teacher-feedback", status_code=201)
async def create_feedback(
    body: FeedbackIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        sa, teacher_id = await _teacher_for(conn, actor, body.student_assignment_id)
        draft_context = None
        if body.ai_draft:
            concepts = await fetch_all(
                conn,
                """SELECT c.name, m.mastery_status AS status FROM lesson_concepts lc JOIN concepts c USING (concept_id)
                     LEFT JOIN student_concept_mastery m ON m.concept_id = lc.concept_id AND m.lesson_id = lc.lesson_id
                                                      AND m.student_id = :st AND m.mastery_scope = 'SCHOOL'
                    WHERE lc.lesson_id = :l ORDER BY lc.display_order""",
                st=sa["student_id"],
                l=sa["lesson_id"],
            )
            note = None
            if body.submission_id:
                s = await fetch_one(conn, "SELECT student_note FROM assignment_submissions WHERE submission_id = :s",
                                    s=body.submission_id)  # fmt: skip
                note = s and s["student_note"]
            draft_context = {"student_first_name": sa["display_name"].split()[0], "assignment_title": sa["title"],
                             "concepts": concepts, "submission_note": note}  # fmt: skip
    request.state.school_id = str(sa["school_id"])
    # The model call happens before the transaction (no DB locks held while waiting on it).
    ai_text = await get_grading_ai().draft_feedback(draft_context) if draft_context else None

    async def handler(conn: AsyncConnection):
        if body.submission_id:
            ok = await fetch_one(
                conn, "SELECT 1 AS x FROM assignment_submissions WHERE submission_id = :s AND student_assignment_id = :sa",
                s=body.submission_id, sa=body.student_assignment_id,
            )  # fmt: skip
            if ok is None:
                raise errors.not_found("submission", submission_id=str(body.submission_id))
        existing = await fetch_one(
            conn,
            """SELECT feedback_id FROM teacher_feedback WHERE teacher_id = :t AND student_id = :st
                  AND assignment_id = :a AND status = 'DRAFT'""",
            t=teacher_id,
            st=sa["student_id"],
            a=sa["assignment_id"],
        )
        if existing:
            raise errors.AppError("FEEDBACK_DRAFT_EXISTS", 409, "You already have a draft for this student.",
                                  {"feedback_id": str(existing["feedback_id"])})  # fmt: skip
        row = await fetch_one(
            conn,
            """INSERT INTO teacher_feedback (teacher_id, student_id, assignment_id, submission_id, feedback_text,
                                             ai_draft_text, status, visible_to_student)
               VALUES (:t, :st, :a, :s, :txt, :ai, 'DRAFT', false) RETURNING *""",
            t=teacher_id,
            st=sa["student_id"],
            a=sa["assignment_id"],
            s=body.submission_id,
            txt=body.feedback_text if body.feedback_text is not None else ai_text,
            ai=ai_text,
        )
        return 201, _view(row)

    return await run_idempotent(request, actor, body, handler, school_id=sa["school_id"])


async def _own_draft(conn: AsyncConnection, actor: Actor, feedback_id: UUID) -> dict:
    row = await fetch_one(conn, "SELECT * FROM teacher_feedback WHERE feedback_id = :f FOR UPDATE", f=feedback_id)
    if row is None:
        raise errors.not_found("teacher_feedback", feedback_id=str(feedback_id))
    sa = await fetch_one(
        conn, "SELECT student_assignment_id FROM student_assignments WHERE assignment_id = :a AND student_id = :s",
        a=row["assignment_id"], s=row["student_id"],
    )  # fmt: skip
    _, teacher_id = await _teacher_for(conn, actor, sa["student_assignment_id"])
    if teacher_id != row["teacher_id"]:
        raise errors.not_found("teacher_feedback", feedback_id=str(feedback_id))
    if row["status"] != "DRAFT":
        raise errors.AppError("FEEDBACK_ALREADY_PUBLISHED", 409, "Published feedback can't be changed.")
    return {**row, "student_assignment_id": sa["student_assignment_id"]}


class FeedbackEdit(BaseModel):
    feedback_text: str = Field(min_length=1, max_length=5000)


@router.patch("/teacher-feedback/{feedback_id}")
async def edit_feedback(
    feedback_id: UUID, body: FeedbackEdit, request: Request, actor: Actor = Depends(current_actor)
) -> JSONResponse:
    async def handler(conn: AsyncConnection):
        await _own_draft(conn, actor, feedback_id)
        row = await fetch_one(
            conn, "UPDATE teacher_feedback SET feedback_text = :t WHERE feedback_id = :f RETURNING *",
            t=body.feedback_text, f=feedback_id,
        )  # fmt: skip
        return 200, _view(row)

    return await run_idempotent(request, actor, body, handler)


@router.post("/teacher-feedback/{feedback_id}/publish")
async def publish_feedback(
    feedback_id: UUID,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async def handler(conn: AsyncConnection):
        draft = await _own_draft(conn, actor, feedback_id)
        if not (draft["feedback_text"] or "").strip():
            raise errors.AppError("FEEDBACK_EMPTY", 409, "Write or accept the feedback text before sending.")
        row = await fetch_one(
            conn,
            """UPDATE teacher_feedback SET status = 'PUBLISHED', visible_to_student = true, published_at = now()
                WHERE feedback_id = :f RETURNING *""",
            f=feedback_id,
        )
        info = await _context(conn, draft["student_assignment_id"])
        await execute(
            conn,
            """INSERT INTO notifications (school_id, recipient_user_id, notification_type, title, priority, entity_type,
                                          entity_id, deep_link)
               VALUES (:s, :u, 'TEACHER_FEEDBACK', :t, 'NORMAL', 'teacher_feedback', :f, :link)""",
            s=info["school_id"],
            u=info["student_user_id"],
            t=f"Feedback on {info['title']}",
            f=feedback_id,
            link=f"aayra://student/assignments/{draft['student_assignment_id']}/feedback",
        )
        await emit(
            conn, ctx, event_type="TeacherFeedbackPublished", aggregate_type="teacher_feedback", aggregate_id=feedback_id,
            school_id=info["school_id"],
            data={"student_id": row["student_id"], "assignment_id": row["assignment_id"],
                  "ai_drafted": row["ai_draft_text"] is not None,
                  "edited_from_ai": row["ai_draft_text"] is not None and row["ai_draft_text"] != row["feedback_text"]},
        )  # fmt: skip
        return 200, _view(row)

    return await run_idempotent(request, actor, {"publish": str(feedback_id)}, handler)


@router.get("/students/me/assignments/{student_assignment_id}/feedback")
async def my_feedback(student_assignment_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    sctx = await student_ctx(actor, request)
    async with get_db().connect() as conn:
        sa = await load_student_assignment(conn, sctx.student_id, student_assignment_id)
        rows = await fetch_all(
            conn,
            """SELECT f.feedback_id, f.feedback_text, f.published_at, u.display_name AS teacher_name
                 FROM teacher_feedback f JOIN teacher_profiles t USING (teacher_id) JOIN users u ON u.user_id = t.user_id
                WHERE f.assignment_id = :a AND f.student_id = :st AND f.visible_to_student
                ORDER BY f.published_at""",
            a=sa["assignment_id"],
            st=sctx.student_id,
        )  # never ai_draft_text: students only see what the teacher approved
    return {"data": {"feedback": rows}, "meta": {"request_id": request.state.request_id}}
