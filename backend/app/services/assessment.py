"""AssessmentService + ReviewEngine (contract §7.4, §7.6).

One pipeline serves both the end-of-lesson mastery check and spaced reviews: a review session
opens a companion `assessment_attempts` row (review_session_id set) and everything else —
question serving, exposure-at-serve-time, TX1/TX2, evidence, mastery — is shared.

Question flow per attempt:
  * A question is "served" by writing student_question_exposures (attempt-linked) BEFORE the
    student answers, so abandoned questions still count as seen.
  * MCQ is graded instantly. A first wrong answer returns a hint and keeps the question open for
    one retry (help_level HINT); a second wrong answer returns a micro-explanation and the next
    pick favours a verification question on the same concept (doc 21 repair path).
  * Free text is accepted (TX1, 202 PENDING) and graded by a RESPONSE_GRADING job (TX2); the next
    question is served immediately, never blocking on the model.
  * The attempt's blueprint and question pool are frozen at start (§7.4).
"""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.ai.grading_ai import get_grading_ai
from app.context import RequestContext
from app.db import execute, fetch_all, fetch_one
from app.outbox import emit
from app.services.mastery import eligibility_for, finalize_evaluation

MAX_MASTERY_QUESTIONS = 8  # doc 21: 6-8 is a useful target, not a hard rule
REVIEW_QUESTIONS = 2
MAX_REVIEW_QUESTIONS = 3
LOW_CONFIDENCE = 0.6


# ------------------------------------------------------------------------------------------------
# reading state
# ------------------------------------------------------------------------------------------------
def student_view(q: dict[str, Any]) -> dict[str, Any]:
    """What a student may see of a question: never the answer key or rubric."""
    view = {"question_id": q["question_id"], "question_type": q["question_type"], "prompt_text": q["prompt_text"]}
    if q["question_type"] == "MCQ":
        view["options"] = (q["answer_spec"] or {}).get("options", [])
    return view


async def load_attempt(conn: AsyncConnection, attempt_id: UUID, *, lock: bool = False) -> dict[str, Any]:
    row = await fetch_one(
        conn, f"SELECT * FROM assessment_attempts WHERE attempt_id = :a{' FOR UPDATE' if lock else ''}", a=attempt_id
    )
    if row is None:
        raise errors.not_found("assessment_attempt", attempt_id=str(attempt_id))
    return row


async def _responses(conn: AsyncConnection, attempt_id: UUID) -> list[dict[str, Any]]:
    """Responses with their CURRENT evaluation (None while pending)."""
    return await fetch_all(
        conn,
        """SELECT r.response_id, r.question_id, r.response_sequence, q.concept_id, q.question_type,
                  re.evaluation, re.help_level
             FROM assessment_responses r
             JOIN questions q ON q.question_id = r.question_id
             LEFT JOIN response_evaluations re ON re.response_id = r.response_id AND re.is_current
            WHERE r.assessment_attempt_id = :a ORDER BY r.response_sequence""",
        a=attempt_id,
    )


async def open_question(conn: AsyncConnection, attempt_id: UUID) -> tuple[dict[str, Any], str] | None:
    """The question currently awaiting an answer, and whether it is a NEW ask or a RETRY."""
    last = await fetch_one(
        conn,
        """SELECT q.* FROM student_question_exposures x JOIN questions q USING (question_id)
            WHERE x.assessment_attempt_id = :a ORDER BY x.asked_at DESC, x.created_at DESC LIMIT 1""",
        a=attempt_id,
    )
    if last is None:
        return None
    answers = [r for r in await _responses(conn, attempt_id) if r["question_id"] == last["question_id"]]
    if not answers:
        return last, "NEW"
    if (
        len(answers) == 1
        and last["question_type"] == "MCQ"
        and answers[0]["evaluation"] in ("INCORRECT", "PARTIAL")
        and answers[0]["help_level"] == "NONE"
    ):
        return last, "RETRY"
    return None


async def pending_count(conn: AsyncConnection, attempt_id: UUID) -> int:
    """Answers still awaiting an evaluation. For parent/self work there is no teacher to fall back on,
    so an answer whose grading permanently failed is left ungraded instead of blocking forever."""
    row = await fetch_one(
        conn,
        """SELECT count(*) AS n FROM assessment_responses r
            WHERE r.assessment_attempt_id = :a
              AND NOT EXISTS (SELECT 1 FROM response_evaluations re WHERE re.response_id = r.response_id AND re.is_current)
              AND NOT EXISTS (
                SELECT 1 FROM background_jobs j
                  JOIN assessment_attempts at ON at.attempt_id = r.assessment_attempt_id
                  JOIN student_assignments sa ON sa.student_assignment_id = at.student_assignment_id
                  JOIN assignments a ON a.assignment_id = sa.assignment_id
                 WHERE j.job_type = 'RESPONSE_GRADING' AND j.entity_id = r.response_id AND j.status = 'FAILED'
                   AND a.source_type <> 'SCHOOL')""",
        a=attempt_id,
    )
    return row["n"]


# ------------------------------------------------------------------------------------------------
# question selection
# ------------------------------------------------------------------------------------------------
async def _concept_plan(conn: AsyncConnection, attempt: dict[str, Any]) -> list[dict[str, Any]]:
    if attempt["review_session_id"]:
        row = await fetch_one(
            conn,
            """SELECT rs.concept_id FROM review_sessions s
                 JOIN review_schedule_items i ON i.review_schedule_item_id = s.review_schedule_item_id
                 JOIN review_schedules rs ON rs.review_schedule_id = i.review_schedule_id
                WHERE s.review_session_id = :s""",
            s=attempt["review_session_id"],
        )
        return [{"concept_id": row["concept_id"], "need": REVIEW_QUESTIONS}]
    rows = await fetch_all(
        conn,
        """SELECT concept_id, required_evidence FROM assessment_blueprint_concepts
            WHERE blueprint_id = :b ORDER BY priority NULLS LAST, concept_id""",
        b=attempt["blueprint_id"],
    )
    return [{"concept_id": r["concept_id"], "need": max(1, int((r["required_evidence"] or {}).get("min_correct", 1)))}
            for r in rows]  # fmt: skip


async def select_next_question(conn: AsyncConnection, attempt: dict[str, Any]) -> dict[str, Any] | None:
    is_review = attempt["review_session_id"] is not None
    responses = await _responses(conn, attempt["attempt_id"])
    served = await fetch_all(
        conn,
        "SELECT question_id FROM student_question_exposures WHERE assessment_attempt_id = :a",
        a=attempt["attempt_id"],
    )
    served_ids = {s["question_id"] for s in served}
    if len(served_ids) >= (MAX_REVIEW_QUESTIONS if is_review else MAX_MASTERY_QUESTIONS):
        return None
    for concept in await _concept_plan(conn, attempt):
        cid = concept["concept_id"]
        asked = {r["question_id"] for r in responses if r["concept_id"] == cid}
        # final outcome per question = last evaluation for it in this attempt
        final: dict[UUID, str | None] = {}
        for r in responses:
            if r["concept_id"] == cid:
                final[r["question_id"]] = r["evaluation"]
        missed = sum(1 for e in final.values() if e in ("INCORRECT", "PARTIAL"))
        target = concept["need"] + min(missed, 1)  # one related verification question after a miss
        if len(asked) >= target:
            continue
        # Prefer questions this student has never seen; then least recently seen (anchor reuse in reviews).
        candidate = await fetch_one(
            conn,
            """SELECT q.*, max(x.asked_at) AS last_seen
                 FROM questions q
                 LEFT JOIN student_question_exposures x ON x.question_id = q.question_id AND x.student_id = :st
                WHERE q.blueprint_id = :b AND q.concept_id = :c AND NOT (q.question_id = ANY(:served))
                GROUP BY q.question_id
                ORDER BY (max(x.asked_at) IS NOT NULL), max(x.asked_at) NULLS FIRST,
                         (q.question_type <> 'MCQ'), q.created_at, q.question_id
                LIMIT 1""",
            st=attempt["student_id"],
            b=attempt["blueprint_id"],
            c=cid,
            served=list(served_ids) or [UUID(int=0)],
        )
        if candidate is None:
            continue
        if candidate["last_seen"] is not None and not is_review:
            continue  # mastery checks only use fresh questions; reviews may reuse anchors
        candidate.pop("last_seen", None)
        return candidate
    return None


async def serve(conn: AsyncConnection, attempt: dict[str, Any], question: dict[str, Any]) -> dict[str, Any]:
    is_review = attempt["review_session_id"] is not None
    await execute(
        conn,
        """INSERT INTO student_question_exposures (student_id, question_id, concept_id, context, asked_at,
                                                   assessment_attempt_id)
           VALUES (:st, :q, :c, :ctx, clock_timestamp(), :a)""",
        st=attempt["student_id"],
        q=question["question_id"],
        c=question["concept_id"],
        ctx="REVIEW" if is_review else "MASTERY_CHECK",
        a=attempt["attempt_id"],
    )
    if is_review:
        seen_before = await fetch_one(
            conn,
            """SELECT count(*) AS n FROM student_question_exposures
                WHERE student_id = :st AND question_id = :q""",
            st=attempt["student_id"],
            q=question["question_id"],
        )
        n = await fetch_one(
            conn, "SELECT count(*) AS n FROM review_session_questions WHERE review_session_id = :s",
            s=attempt["review_session_id"],
        )  # fmt: skip
        await execute(
            conn,
            """INSERT INTO review_session_questions (review_session_id, question_id, sequence_number, is_anchor_reuse)
               VALUES (:s, :q, :n, :reuse)""",
            s=attempt["review_session_id"],
            q=question["question_id"],
            n=n["n"] + 1,
            reuse=seen_before["n"] > 1,
        )
    return student_view(question)


# ------------------------------------------------------------------------------------------------
# advancing / completion
# ------------------------------------------------------------------------------------------------
async def advance(conn: AsyncConnection, ctx: RequestContext, attempt_id: UUID) -> dict[str, Any]:
    """After any state change: keep an open question, serve the next one, or finish."""
    attempt = await load_attempt(conn, attempt_id, lock=True)
    if attempt["status"] == "COMPLETED":
        return {"type": "COMPLETED", "result": await attempt_result(conn, attempt_id)}
    current = await open_question(conn, attempt_id)
    if current:
        q, mode = current
        return {"type": "RETRY" if mode == "RETRY" else "QUESTION", "question": student_view(q)}
    nxt = await select_next_question(conn, attempt)
    if nxt is not None:
        if attempt["status"] != "IN_PROGRESS":
            await execute(
                conn, "UPDATE assessment_attempts SET status = 'IN_PROGRESS' WHERE attempt_id = :a", a=attempt_id
            )
        return {"type": "QUESTION", "question": await serve(conn, attempt, nxt)}
    if await pending_count(conn, attempt_id):
        await execute(conn, "UPDATE assessment_attempts SET status = 'AWAITING_EVALUATION' WHERE attempt_id = :a",
                      a=attempt_id)  # fmt: skip
        return {"type": "AWAITING_EVALUATION"}
    await complete_attempt(conn, ctx, attempt)
    return {"type": "COMPLETED", "result": await attempt_result(conn, attempt_id)}


async def _school_today(conn: AsyncConnection, lesson_id: UUID) -> tuple[date, date]:
    row = await fetch_one(
        conn,
        """SELECT s.timezone, ay.end_date FROM lessons l JOIN schools s USING (school_id)
             JOIN academic_years ay ON ay.academic_year_id = l.academic_year_id WHERE l.lesson_id = :l""",
        l=lesson_id,
    )
    return datetime.now(UTC).astimezone(ZoneInfo(row["timezone"])).date(), row["end_date"]


async def complete_attempt(conn: AsyncConnection, ctx: RequestContext, attempt: dict[str, Any]) -> None:
    responses = await _responses(conn, attempt["attempt_id"])
    plan = await _concept_plan(conn, attempt)
    correct_concepts = {r["concept_id"] for r in responses if r["evaluation"] == "CORRECT"}
    stop_reason = "SUFFICIENT_EVIDENCE" if all(c["concept_id"] in correct_concepts for c in plan) else "NEEDS_REVIEW"
    await execute(
        conn,
        """UPDATE assessment_attempts SET status = 'COMPLETED', completed_at = now(), stop_reason = :r
            WHERE attempt_id = :a""",
        r=stop_reason,
        a=attempt["attempt_id"],
    )
    lesson = await fetch_one(conn, "SELECT school_id FROM lessons WHERE lesson_id = :l", l=attempt["lesson_id"])
    if attempt["review_session_id"]:
        await _complete_review(conn, ctx, attempt, stop_reason, lesson["school_id"])
        return

    await execute(
        conn,
        """UPDATE student_lesson_progress SET status = 'COMPLETED', last_activity_at = now()
            WHERE student_assignment_id = :sa""",
        sa=attempt["student_assignment_id"],
    )
    if attempt["student_assignment_id"]:
        await maybe_complete_assignment(conn, attempt["student_assignment_id"])
    # Seed spaced review: Day 2 for every concept whose evidence here was review-eligible (§7.4).
    today, year_end = await _school_today(conn, attempt["lesson_id"])
    eligible = await fetch_all(
        conn,
        """SELECT DISTINCT e.concept_id FROM evidence_events e
             JOIN response_evaluations re ON re.response_evaluation_id = e.response_evaluation_id
             JOIN assessment_responses r ON r.response_id = re.response_id
            WHERE r.assessment_attempt_id = :a AND e.review_eligible""",
        a=attempt["attempt_id"],
    )
    lesson_year = await fetch_one(
        conn, "SELECT academic_year_id FROM lessons WHERE lesson_id = :l", l=attempt["lesson_id"]
    )
    for row in eligible:
        schedule = await fetch_one(
            conn,
            """INSERT INTO review_schedules (student_id, lesson_id, concept_id, academic_year_id, anchor_date, status)
               VALUES (:st, :l, :c, :y, :d, 'ACTIVE')
               ON CONFLICT (student_id, lesson_id, concept_id, academic_year_id) DO NOTHING
               RETURNING review_schedule_id""",
            st=attempt["student_id"],
            l=attempt["lesson_id"],
            c=row["concept_id"],
            y=lesson_year["academic_year_id"],
            d=today,
        )
        if schedule and today + timedelta(days=2) <= year_end:
            await execute(
                conn,
                """INSERT INTO review_schedule_items (review_schedule_id, review_day, scheduled_for, status)
                   VALUES (:s, 2, :d, 'UPCOMING') ON CONFLICT DO NOTHING""",
                s=schedule["review_schedule_id"],
                d=today + timedelta(days=2),
            )
    await emit(
        conn,
        ctx,
        event_type="AssessmentCompleted",
        aggregate_type="assessment_attempt",
        aggregate_id=attempt["attempt_id"],
        school_id=lesson["school_id"],
        data={
            "student_id": attempt["student_id"],
            "lesson_id": attempt["lesson_id"],
            "stop_reason": stop_reason,
            "reviews_seeded": len(eligible),
        },  # fmt: skip
    )


async def maybe_complete_assignment(conn: AsyncConnection, student_assignment_id: UUID) -> str | None:
    """Mark the student's assignment done once its completion_mode is satisfied (contract §7.4):
    LEARNING_COMPLETION -> mastery check done; SUBMISSION_REQUIRED -> a submission exists;
    BOTH -> both. Late work is labelled SUBMITTED_LATE (doc 21: late allowed, never auto-penalised)."""
    sa = await fetch_one(
        conn,
        """SELECT sa.*, a.completion_mode, a.due_at FROM student_assignments sa
             JOIN assignments a USING (assignment_id) WHERE sa.student_assignment_id = :sa FOR UPDATE OF sa""",
        sa=student_assignment_id,
    )
    if sa is None or sa["status"] in ("COMPLETED", "SUBMITTED", "SUBMITTED_LATE"):
        return None
    checked = await fetch_one(
        conn,
        """SELECT 1 AS x FROM assessment_attempts WHERE student_assignment_id = :sa AND review_session_id IS NULL
              AND status = 'COMPLETED' LIMIT 1""",
        sa=student_assignment_id,
    )
    submitted = await fetch_one(
        conn,
        "SELECT 1 AS x FROM assignment_submissions WHERE student_assignment_id = :sa LIMIT 1",
        sa=student_assignment_id,
    )
    mode = sa["completion_mode"]
    done = (
        (mode == "LEARNING_COMPLETION" and checked)
        or (mode == "SUBMISSION_REQUIRED" and submitted)
        or (mode == "BOTH" and checked and submitted)
    )
    if not done:
        return None
    due = sa["effective_due_at"] or sa["due_at"]
    late = due is not None and datetime.now(UTC) > due
    status = "SUBMITTED_LATE" if late else ("SUBMITTED" if mode == "SUBMISSION_REQUIRED" else "COMPLETED")
    await execute(
        conn,
        """UPDATE student_assignments SET status = :st, completed_at = now(), submitted_at = CASE WHEN :sub THEN now()
                  ELSE submitted_at END, is_late = :late, version = version + 1
            WHERE student_assignment_id = :sa""",
        st=status,
        sub=bool(submitted),
        late=late,
        sa=student_assignment_id,
    )
    return status


async def _complete_review(
    conn: AsyncConnection, ctx: RequestContext, attempt: dict[str, Any], stop_reason: str, school_id: UUID
) -> None:
    """Fixed cadence (doc 21, Sept 16): the next review is ALWAYS review_day x 2, whatever the outcome.
    Performance only changes what the next review emphasises, never when it happens."""
    session = await fetch_one(
        conn,
        """SELECT s.review_session_id, i.review_schedule_item_id, i.review_day, rs.review_schedule_id, rs.anchor_date
             FROM review_sessions s
             JOIN review_schedule_items i ON i.review_schedule_item_id = s.review_schedule_item_id
             JOIN review_schedules rs ON rs.review_schedule_id = i.review_schedule_id
            WHERE s.review_session_id = :s""",
        s=attempt["review_session_id"],
    )
    await execute(
        conn,
        "UPDATE review_sessions SET status = 'COMPLETED', completed_at = now(), stop_reason = :r WHERE review_session_id = :s",
        r=stop_reason,
        s=session["review_session_id"],
    )
    await execute(
        conn,
        """UPDATE review_schedule_items SET status = 'COMPLETED', completed_at = now(), review_session_id = :s
            WHERE review_schedule_item_id = :i""",
        s=session["review_session_id"],
        i=session["review_schedule_item_id"],
    )
    _, year_end = await _school_today(conn, attempt["lesson_id"])
    next_day = session["review_day"] * 2
    next_date = session["anchor_date"] + timedelta(days=next_day)
    if next_date <= year_end:
        await execute(
            conn,
            """INSERT INTO review_schedule_items (review_schedule_id, review_day, scheduled_for, status)
               VALUES (:s, :d, :at, 'UPCOMING') ON CONFLICT DO NOTHING""",
            s=session["review_schedule_id"],
            d=next_day,
            at=next_date,
        )
    else:
        await execute(
            conn, "UPDATE review_schedules SET status = 'COMPLETED' WHERE review_schedule_id = :s",
            s=session["review_schedule_id"],
        )  # fmt: skip
    await emit(
        conn,
        ctx,
        event_type="ReviewCompleted",
        aggregate_type="review_session",
        aggregate_id=session["review_session_id"],
        school_id=school_id,
        data={
            "student_id": attempt["student_id"],
            "review_day": session["review_day"],
            "stop_reason": stop_reason,
            "next_review_day": next_day if next_date <= year_end else None,
        },  # fmt: skip
    )


async def attempt_result(conn: AsyncConnection, attempt_id: UUID) -> dict[str, Any]:
    """Concise, student-facing summary (doc 21: concise AI feedback for students; no ranking)."""
    attempt = await load_attempt(conn, attempt_id)
    responses = await _responses(conn, attempt_id)
    scope = (await eligibility_for(conn, attempt["student_assignment_id"])).scope
    concepts = await fetch_all(
        conn,
        """SELECT c.concept_id, c.name, m.mastery_status FROM concepts c
             LEFT JOIN student_concept_mastery m ON m.concept_id = c.concept_id AND m.student_id = :st
                                                AND m.lesson_id = :l AND m.mastery_scope = :scope
            WHERE c.concept_id = ANY(:ids) ORDER BY c.name""",
        st=attempt["student_id"],
        l=attempt["lesson_id"],
        scope=scope,
        ids=list({r["concept_id"] for r in responses}) or [UUID(int=0)],
    )
    return {
        "stop_reason": attempt["stop_reason"],
        "questions_answered": len({r["question_id"] for r in responses}),
        "correct": sum(1 for r in responses if r["evaluation"] == "CORRECT"),
        "concepts": [
            {"concept_id": c["concept_id"], "name": c["name"], "status": c["mastery_status"]} for c in concepts
        ],
    }


# ------------------------------------------------------------------------------------------------
# answering (TX1) and grading (TX2)
# ------------------------------------------------------------------------------------------------
def _validate_answer(question: dict[str, Any], answer: dict[str, Any]) -> None:
    if question["question_type"] == "MCQ":
        idx = answer.get("selected_index")
        options = (question["answer_spec"] or {}).get("options", [])
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(options):
            raise errors.validation_error("MCQ answers need selected_index within the options.", field="answer")
    else:
        text = answer.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise errors.validation_error("Written answers need non-empty text (max 5000 chars).", field="answer")


async def submit_response(
    conn: AsyncConnection, ctx: RequestContext, attempt_id: UUID, student_id: UUID, question_id: UUID,
    answer: dict[str, Any],
) -> tuple[int, dict[str, Any]]:  # fmt: skip
    attempt = await load_attempt(conn, attempt_id, lock=True)
    if attempt["student_id"] != student_id:
        raise errors.not_found("assessment_attempt", attempt_id=str(attempt_id))
    if attempt["status"] != "IN_PROGRESS":
        raise errors.AppError("ASSESSMENT_NOT_ACTIVE", 409, "This check is not accepting answers.",
                              {"status": attempt["status"]})  # fmt: skip
    current = await open_question(conn, attempt_id)
    if current is None or current[0]["question_id"] != question_id:
        answered = await fetch_one(
            conn, "SELECT 1 AS x FROM assessment_responses WHERE assessment_attempt_id = :a AND question_id = :q",
            a=attempt_id, q=question_id,
        )  # fmt: skip
        if answered:
            raise errors.AppError("QUESTION_ALREADY_ANSWERED", 409, "This question was already answered.")
        raise errors.AppError("QUESTION_NOT_CURRENT", 409, "Answer the current question.",
                              {"current_question_id": str(current[0]["question_id"]) if current else None})  # fmt: skip
    question, mode = current
    _validate_answer(question, answer)
    seq = await fetch_one(
        conn, "SELECT coalesce(max(response_sequence), 0) + 1 AS n FROM assessment_responses WHERE assessment_attempt_id = :a",
        a=attempt_id,
    )  # fmt: skip
    response = await fetch_one(
        conn,
        """INSERT INTO assessment_responses (assessment_attempt_id, question_id, response_sequence, response_payload,
                                             answered_at)
           VALUES (:a, :q, :n, CAST(:p AS jsonb), now()) RETURNING response_id""",
        a=attempt_id,
        q=question_id,
        n=seq["n"],
        p=json.dumps(answer),
    )
    context, review_day = await _evidence_context(conn, attempt)

    if question["question_type"] == "MCQ":
        correct = answer["selected_index"] == question["answer_spec"].get("correct_index")
        evaluation = "CORRECT" if correct else "INCORRECT"
        help_level = "HINT" if mode == "RETRY" else "NONE"
        await finalize_evaluation(
            conn, ctx, attempt=attempt, response=response, question=question, evaluation=evaluation,
            confidence=1.0, help_level=help_level, evaluated_by="AI", evidence_context=context, review_day=review_day,
        )  # fmt: skip
        concept = await fetch_one(conn, "SELECT name FROM concepts WHERE concept_id = :c", c=question["concept_id"])
        ai = get_grading_ai()
        feedback: dict[str, Any] = {"kind": "CORRECT"}
        if not correct:
            feedback = (
                {"kind": "HINT", "text": ai.hint(question, concept["name"])}
                if mode == "NEW"
                else {"kind": "EXPLANATION", "text": ai.explanation(question, concept["name"])}
            )
        nxt = await advance(conn, ctx, attempt_id)
        return 200, {
            "response_id": response["response_id"],
            "evaluation_status": "EVALUATED",
            "evaluation": evaluation,
            "feedback": feedback,
            "next": nxt,
        }

    # Free text: accept now, grade asynchronously (never hold the request open on a model call).
    await execute(
        conn,
        """INSERT INTO background_jobs (school_id, job_type, entity_type, entity_id, status, max_attempts)
           SELECT l.school_id, 'RESPONSE_GRADING', 'assessment_response', :r, 'QUEUED', 3
             FROM lessons l WHERE l.lesson_id = :l""",
        r=response["response_id"],
        l=attempt["lesson_id"],
    )
    nxt = await advance(conn, ctx, attempt_id)
    return 202, {"response_id": response["response_id"], "evaluation_status": "PENDING", "next": nxt}


async def _evidence_context(conn: AsyncConnection, attempt: dict[str, Any]) -> tuple[str, int | None]:
    if not attempt["review_session_id"]:
        return "INITIAL_MASTERY", None
    row = await fetch_one(
        conn,
        """SELECT i.review_day FROM review_sessions s JOIN review_schedule_items i USING (review_schedule_item_id)
            WHERE s.review_session_id = :s""",
        s=attempt["review_session_id"],
    )
    return "SPACED_REVIEW", row["review_day"]


async def responsible_teacher(conn: AsyncConnection, attempt: dict[str, Any]) -> UUID | None:
    row = await fetch_one(
        conn,
        """SELECT t.teacher_id FROM student_enrollments se
             JOIN lessons l ON l.lesson_id = :l
             JOIN teacher_section_subject_assignments t
               ON t.section_id = se.section_id AND t.subject_id = l.subject_id AND t.status = 'ACTIVE'
              AND t.assignment_role IN ('PRIMARY_SUBJECT_TEACHER', 'CO_TEACHER')
            WHERE se.student_id = :st AND se.status = 'ACTIVE' AND se.academic_year_id = l.academic_year_id
            ORDER BY (t.assignment_role = 'PRIMARY_SUBJECT_TEACHER') DESC LIMIT 1""",
        l=attempt["lesson_id"],
        st=attempt["student_id"],
    )
    if row:
        return row["teacher_id"]
    fallback = await fetch_one(
        conn,
        """SELECT tp.teacher_id FROM student_assignments sa JOIN assignments a USING (assignment_id)
             JOIN teacher_profiles tp ON tp.user_id = a.created_by WHERE sa.student_assignment_id = :sa""",
        sa=attempt["student_assignment_id"],
    )
    return fallback["teacher_id"] if fallback else None


async def queue_teacher_review(
    conn: AsyncConnection, ctx: RequestContext, attempt: dict[str, Any], *, response_evaluation_id: UUID | None,
    reason: str, snapshot: dict[str, Any],
) -> bool:  # fmt: skip
    """School work only: parent/self learning never lands in a teacher's queue."""
    if (await eligibility_for(conn, attempt["student_assignment_id"])).scope != "SCHOOL":
        return False
    teacher_id = await responsible_teacher(conn, attempt)
    if teacher_id is None:
        return False
    lesson = await fetch_one(conn, "SELECT school_id FROM lessons WHERE lesson_id = :l", l=attempt["lesson_id"])
    sa = await fetch_one(
        conn, "SELECT assignment_id FROM student_assignments WHERE student_assignment_id = :sa",
        sa=attempt["student_assignment_id"],
    )  # fmt: skip
    item = await fetch_one(
        conn,
        """INSERT INTO teacher_review_items (school_id, teacher_id, student_id, assignment_id, response_evaluation_id,
                                             reason_type, status, ai_evaluation_snapshot)
           VALUES (:s, :t, :st, :a, :re, :reason, 'PENDING', CAST(:snap AS jsonb)) RETURNING review_item_id""",
        s=lesson["school_id"],
        t=teacher_id,
        st=attempt["student_id"],
        a=sa["assignment_id"] if sa else None,
        re=response_evaluation_id,
        reason=reason,
        snap=json.dumps(snapshot, default=str),
    )
    await emit(
        conn,
        ctx,
        event_type="TeacherReviewRequired",
        aggregate_type="teacher_review_item",
        aggregate_id=item["review_item_id"],
        school_id=lesson["school_id"],
        data={"teacher_id": teacher_id, "student_id": attempt["student_id"], "reason": reason},
    )
    return True
