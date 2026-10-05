"""TX2 for free-text answers (contract §7.4): a RESPONSE_GRADING job judges the answer, turns it into
evidence + mastery in one transaction, routes low-confidence results to the teacher, and finishes
the attempt if it was only waiting on this evaluation.

If the model keeps failing, the job does not fail silently: after the last retry a MANUAL_REVIEW
item goes to the teacher (§14 "AI evaluation times out").
"""

import time
from typing import Any

from app.ai.grading_ai import GradingUnavailable, get_grading_ai
from app.db import Database, execute, fetch_one
from app.services.assessment import (
    LOW_CONFIDENCE,
    _evidence_context,
    advance,
    load_attempt,
    queue_teacher_review,
)
from app.services.mastery import finalize_evaluation
from app.services.pipeline import system_context


async def _log(conn, *, school_id, student_id, response_id, latency_ms, status, tokens=(None, None)) -> None:
    ai = get_grading_ai()
    await execute(
        conn,
        """INSERT INTO ai_invocations (school_id, student_id, purpose, entity_type, entity_id, model_name,
                                       prompt_version, input_token_count, output_token_count, latency_ms, status)
           VALUES (:s, :st, 'RESPONSE_EVALUATION', 'assessment_response', :r, :m, :pv, :it, :ot, :lat, :status)""",
        s=school_id,
        st=student_id,
        r=response_id,
        m=ai.model_name,
        pv=ai.prompt_version,
        it=tokens[0],
        ot=tokens[1],
        lat=latency_ms,
        status=status,
    )


async def run_grading_job(db: Database, job: dict[str, Any]) -> None:
    ctx = system_context(job["job_id"])
    async with db.begin() as conn:
        response = await fetch_one(
            conn, "SELECT * FROM assessment_responses WHERE response_id = :r", r=job["entity_id"]
        )
        attempt = await load_attempt(conn, response["assessment_attempt_id"], lock=True)
        current = await fetch_one(
            conn, "SELECT 1 AS x FROM response_evaluations WHERE response_id = :r AND is_current",
            r=response["response_id"],
        )  # fmt: skip
        if current:
            return  # already evaluated (e.g. a teacher resolved it first)
        question = await fetch_one(conn, "SELECT * FROM questions WHERE question_id = :q", q=response["question_id"])
        lesson = await fetch_one(conn, "SELECT school_id FROM lessons WHERE lesson_id = :l", l=attempt["lesson_id"])
        answer_text = (response["response_payload"] or {}).get("text", "")
        started = time.perf_counter()
        try:
            judgement = await get_grading_ai().judge(question, answer_text)
        except GradingUnavailable as exc:
            await _log(conn, school_id=lesson["school_id"], student_id=attempt["student_id"],
                       response_id=response["response_id"], latency_ms=int((time.perf_counter() - started) * 1000),
                       status="FAILED")  # fmt: skip
            if job["attempt_count"] < job["max_attempts"]:
                raise  # worker marks RETRYING (the failure log above is committed only on the last try)
            await queue_teacher_review(
                conn, ctx, attempt, response_evaluation_id=None, reason="MANUAL_REVIEW",
                snapshot={"status": "UNAVAILABLE", "error": str(exc), "response_id": response["response_id"],
                          "question_id": question["question_id"], "answer": answer_text},
            )  # fmt: skip
            await execute(
                conn, "UPDATE background_jobs SET status = 'FAILED', last_error = :e WHERE job_id = :j",
                e=f"grading unavailable after {job['attempt_count']} attempts: {exc}", j=job["job_id"],
            )  # fmt: skip
            return
        await _log(conn, school_id=lesson["school_id"], student_id=attempt["student_id"],
                   response_id=response["response_id"], latency_ms=int((time.perf_counter() - started) * 1000),
                   status="SUCCEEDED", tokens=(judgement.input_tokens, judgement.output_tokens))  # fmt: skip
        context, review_day = await _evidence_context(conn, attempt)
        result = await finalize_evaluation(
            conn, ctx, attempt=attempt, response=response, question=question, evaluation=judgement.evaluation,
            confidence=judgement.confidence, help_level="NONE", evaluated_by="AI", evidence_context=context,
            review_day=review_day,
        )  # fmt: skip
        if judgement.confidence < LOW_CONFIDENCE:
            await queue_teacher_review(
                conn, ctx, attempt, response_evaluation_id=result["response_evaluation_id"], reason="LOW_CONFIDENCE",
                snapshot={"evaluation": judgement.evaluation, "confidence": judgement.confidence,
                          "feedback": judgement.feedback, "question_id": question["question_id"],
                          "answer": answer_text},
            )  # fmt: skip
        if attempt["status"] == "AWAITING_EVALUATION":
            await advance(conn, ctx, attempt["attempt_id"])
