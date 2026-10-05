"""Contract §7.5 — teacher review queue: confirm or adjust AI evaluations.

`confirm` touches only the queue item. `adjust` writes a NEW teacher evaluation (the AI one stays
in history, no longer current), appends fresh evidence with a fresh eligibility snapshot, and
re-runs mastery. Authority is re-checked on every call (contract §3, §14).
"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.auth import Actor, current_actor
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one, get_db
from app.idempotency import run_idempotent
from app.outbox import emit
from app.services import assessment as svc
from app.services.mastery import finalize_evaluation
from app.services.teaching import require_subject_authority, require_teacher

router = APIRouter(prefix="/v1", tags=["teacher-review"])


async def _load_item(conn: AsyncConnection, actor: Actor, review_item_id: UUID, *, lock: bool = False) -> dict:
    item = await fetch_one(
        conn,
        f"""SELECT i.*, r.response_id, r.assessment_attempt_id AS attempt_id, r.question_id, r.response_payload,
                   re.help_level AS ai_help_level
              FROM teacher_review_items i
              LEFT JOIN response_evaluations re ON re.response_evaluation_id = i.response_evaluation_id
              LEFT JOIN assessment_responses r ON r.response_id = coalesce(re.response_id,
                    CAST(i.ai_evaluation_snapshot ->> 'response_id' AS uuid))
             WHERE i.review_item_id = :i{" FOR UPDATE OF i" if lock else ""}""",
        i=review_item_id,
    )
    teacher = await require_teacher(conn, actor)
    if item is None or item["teacher_id"] != teacher.teacher_id:
        raise errors.not_found("teacher_review_item", review_item_id=str(review_item_id))
    lesson = await fetch_one(
        conn,
        "SELECT l.subject_id, l.academic_year_id FROM assessment_attempts a JOIN lessons l USING (lesson_id) WHERE a.attempt_id = :a",
        a=item["attempt_id"],
    )
    await require_subject_authority(conn, actor, lesson["subject_id"], lesson["academic_year_id"])
    return item


@router.get("/teacher/review-items")
async def list_review_items(request: Request, status: str = "PENDING", actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        teacher = await require_teacher(conn, actor)
        items = await fetch_all(
            conn,
            """SELECT i.review_item_id, i.reason_type, i.status, i.created_at, i.ai_evaluation_snapshot,
                      u.display_name AS student_name, i.student_id, a.title AS assignment_title, q.prompt_text,
                      q.question_type
                 FROM teacher_review_items i
                 JOIN student_profiles sp ON sp.student_id = i.student_id JOIN users u ON u.user_id = sp.user_id
                 LEFT JOIN assignments a ON a.assignment_id = i.assignment_id
                 LEFT JOIN questions q ON q.question_id = CAST(i.ai_evaluation_snapshot ->> 'question_id' AS uuid)
                WHERE i.teacher_id = :t AND i.status = :s
                ORDER BY i.created_at""",
            t=teacher.teacher_id,
            s=status,
        )
    return {"data": {"items": items, "count": len(items)}, "meta": {"request_id": request.state.request_id}}


@router.post("/teacher-review-items/{review_item_id}/confirm")
async def confirm(
    review_item_id: UUID,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async def handler(conn: AsyncConnection):
        item = await _load_item(conn, actor, review_item_id, lock=True)
        if item["status"] != "PENDING":
            raise errors.AppError(
                "REVIEW_ITEM_CLOSED", 409, "This item was already handled.", {"status": item["status"]}
            )
        if item["response_evaluation_id"] is None:
            raise errors.AppError("NOTHING_TO_CONFIRM", 409, "There is no AI evaluation to confirm; adjust instead.")
        await execute(
            conn, "UPDATE teacher_review_items SET status = 'CONFIRMED', reviewed_at = now() WHERE review_item_id = :i",
            i=review_item_id,
        )  # fmt: skip
        return 200, {"review_item_id": review_item_id, "status": "CONFIRMED"}

    return await run_idempotent(request, actor, {"confirm": str(review_item_id)}, handler)


class AdjustIn(BaseModel):
    evaluation: Literal["CORRECT", "PARTIAL", "INCORRECT"]


@router.post("/teacher-review-items/{review_item_id}/adjust")
async def adjust(
    review_item_id: UUID,
    body: AdjustIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async def handler(conn: AsyncConnection):
        item = await _load_item(conn, actor, review_item_id, lock=True)
        if item["status"] != "PENDING":
            raise errors.AppError(
                "REVIEW_ITEM_CLOSED", 409, "This item was already handled.", {"status": item["status"]}
            )
        attempt = await svc.load_attempt(conn, item["attempt_id"], lock=True)
        question = await fetch_one(conn, "SELECT * FROM questions WHERE question_id = :q", q=item["question_id"])
        result = await finalize_evaluation(
            conn, ctx, attempt=attempt, response={"response_id": item["response_id"]}, question=question,
            evaluation=body.evaluation, confidence=None, help_level=item["ai_help_level"] or "NONE",
            evaluated_by="TEACHER", evidence_context="TEACHER_EVALUATION",
        )  # fmt: skip
        await execute(
            conn, "UPDATE teacher_review_items SET status = 'ADJUSTED', reviewed_at = now() WHERE review_item_id = :i",
            i=review_item_id,
        )  # fmt: skip
        await execute(
            conn, "UPDATE background_jobs SET status = 'CANCELLED' WHERE job_type = 'RESPONSE_GRADING' AND entity_id = :r AND status IN ('QUEUED', 'RETRYING')",
            r=item["response_id"],
        )  # fmt: skip
        await emit(
            conn, ctx, event_type="EvaluationAdjusted", aggregate_type="assessment_response",
            aggregate_id=item["response_id"], school_id=item["school_id"],
            data={"review_item_id": review_item_id, "evaluation": body.evaluation, "teacher_id": item["teacher_id"]},
        )  # fmt: skip
        if attempt["status"] == "AWAITING_EVALUATION":
            await svc.advance(conn, ctx, attempt["attempt_id"])
        return 200, {
            "review_item_id": review_item_id,
            "status": "ADJUSTED",
            "evaluation": body.evaluation,
            "mastery_change": result["mastery_change"],
        }

    return await run_idempotent(request, actor, body, handler)
