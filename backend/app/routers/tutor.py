"""In-lesson AI tutor (docs 10/11 MVP). Text now; the web app adds voice with the browser's own
speech recognition and read-aloud, so a voice message arrives here as text with input_mode=VOICE.

Rules:
- Only for a lesson the student is assigned (school, parent or self work).
- Grounded in the lesson's own material: the most relevant evidence chunks are sent with each turn.
- Paused while a mastery check or review for this lesson is in progress (409 TUTOR_PAUSED_DURING_CHECK),
  so it can't be used to answer assessment questions.
- Private: conversations are never shown to parents or teachers.
- A daily message cap per student keeps cost bounded (429 TUTOR_DAILY_LIMIT).
"""

import re
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.ai.tutor_ai import TutorUnavailable, get_tutor_ai
from app.auth import Actor, current_actor
from app.db import execute, fetch_all, fetch_one, get_db
from app.routers.learning import load_student_assignment, student_ctx

router = APIRouter(prefix="/v1", tags=["tutor"])

DAILY_LIMIT = 80
MAX_CHUNKS = 6
HISTORY = 10
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = set("the and for with that this what why how are was were you your can does from into about have has its "
            "which when where who will would should could there their them then than also not but more".split())  # fmt: skip


def rank_chunks(message: str, chunks: list[dict], k: int = MAX_CHUNKS) -> list[dict]:
    """Keyword-overlap ranking (cheap, no embeddings). Ties keep reading order."""
    words = {w for w in _WORD.findall(message.lower()) if w not in _STOP}
    scored = []
    for i, c in enumerate(chunks):
        text = set(_WORD.findall(c["content_text"].lower()))
        scored.append((-len(words & text), i, c))
    best = [c for score, _, c in sorted(scored)[:k] if score < 0]
    return best or chunks[:k]


class TutorIn(BaseModel):
    student_assignment_id: UUID
    message: str = Field(min_length=1, max_length=2000)
    mode: Literal["ASK", "EXPLAIN", "EXAMPLE", "HINT", "PRACTICE"] = "ASK"
    conversation_id: UUID | None = None
    input_mode: Literal["TEXT", "VOICE"] = "TEXT"


async def _lesson_for(conn: AsyncConnection, student_id: UUID, sa_id: UUID, lesson_id: UUID) -> dict:
    sa = await load_student_assignment(conn, student_id, sa_id)
    if sa["lesson_id"] != lesson_id:
        raise errors.not_found("lesson", lesson_id=str(lesson_id))
    return sa


@router.post("/lessons/{lesson_id}/tutor")
async def ask_tutor(lesson_id: UUID, body: TutorIn, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    sctx = await student_ctx(actor, request)
    async with get_db().connect() as conn:
        await _lesson_for(conn, sctx.student_id, body.student_assignment_id, lesson_id)
        active = await fetch_one(
            conn,
            "SELECT 1 AS x FROM assessment_attempts WHERE student_id = :s AND lesson_id = :l AND status = 'IN_PROGRESS'",
            s=sctx.student_id,
            l=lesson_id,
        )
        if active:
            raise errors.AppError(
                "TUTOR_PAUSED_DURING_CHECK",
                409,
                "The tutor is paused while you do the check for this lesson. You've got this!",
            )
        today = await fetch_one(
            conn,
            """SELECT count(*) AS n FROM tutor_messages m JOIN tutor_conversations c USING (conversation_id)
                WHERE c.student_id = :s AND m.role = 'STUDENT' AND m.created_at > now() - interval '1 day'""",
            s=sctx.student_id,
        )
        if today["n"] >= DAILY_LIMIT:
            raise errors.AppError("TUTOR_DAILY_LIMIT", 429, "You've reached today's tutor limit. Try again tomorrow.")
        lesson = await fetch_one(
            conn,
            """SELECT l.title, s.name AS subject_name FROM lessons l JOIN subjects s USING (subject_id)
                WHERE l.lesson_id = :l""",
            l=lesson_id,
        )
        chunks = await fetch_all(
            conn,
            """SELECT ch.chunk_id, ch.content_text FROM content_evidence_chunks ch
                 JOIN lesson_content lc ON lc.content_item_id = ch.content_item_id
                WHERE lc.lesson_id = :l ORDER BY lc.display_order, ch.page_number NULLS LAST, ch.start_seconds NULLS LAST,
                      ch.created_at""",
            l=lesson_id,
        )
        concepts = await fetch_all(
            conn,
            """SELECT c.name FROM lesson_concepts lc JOIN concepts c USING (concept_id)
                WHERE lc.lesson_id = :l ORDER BY lc.display_order""",
            l=lesson_id,
        )
        history: list[dict] = []
        if body.conversation_id:
            conv = await fetch_one(
                conn, "SELECT * FROM tutor_conversations WHERE conversation_id = :c AND student_id = :s AND lesson_id = :l",
                c=body.conversation_id, s=sctx.student_id, l=lesson_id,
            )  # fmt: skip
            if conv is None:
                raise errors.not_found("tutor_conversation", conversation_id=str(body.conversation_id))
            history = list(reversed(await fetch_all(
                conn, """SELECT role, content FROM tutor_messages WHERE conversation_id = :c
                          ORDER BY created_at DESC LIMIT :n""", c=body.conversation_id, n=HISTORY,
            )))  # fmt: skip

    picked = rank_chunks(body.message, chunks)
    ai = get_tutor_ai()
    started = time.perf_counter()
    try:  # model call outside any transaction
        reply = await ai.reply({
            "lesson_title": lesson["title"], "subject_name": lesson["subject_name"],
            "concepts": [c["name"] for c in concepts], "chunks": [c["content_text"] for c in picked],
            "history": history, "message": body.message, "mode": body.mode,
        })  # fmt: skip
        status = "SUCCEEDED"
    except TutorUnavailable:
        reply, status = None, "FAILED"
    latency = int((time.perf_counter() - started) * 1000)

    async with get_db().begin() as conn:
        await execute(
            conn,
            """INSERT INTO ai_invocations (school_id, user_id, student_id, purpose, entity_type, entity_id, model_name,
                                           prompt_version, input_token_count, output_token_count, latency_ms, status,
                                           guardrail_result)
               VALUES (:sc, :u, :st, 'TUTOR_REPLY', 'lesson', :l, :m, :pv, :it, :ot, :lat, :status, CAST(:g AS jsonb))""",
            sc=sctx.school_id, u=actor.user_id, st=sctx.student_id, l=lesson_id, m=ai.model_name, pv=ai.prompt_version,
            it=reply.usage.input_tokens if reply and reply.usage else None,
            ot=reply.usage.output_tokens if reply and reply.usage else None, lat=latency, status=status,
            g='{"off_topic": true}' if reply and reply.off_topic else None,
        )  # fmt: skip
        if reply is None:
            raise errors.AppError(
                "TUTOR_UNAVAILABLE", 503, "The tutor is busy right now. Please try again in a moment."
            )
        conv_id = body.conversation_id
        if conv_id is None:
            conv = await fetch_one(
                conn,
                """INSERT INTO tutor_conversations (school_id, student_id, lesson_id, student_assignment_id)
                   VALUES (:sc, :st, :l, :sa) RETURNING conversation_id""",
                sc=sctx.school_id, st=sctx.student_id, l=lesson_id, sa=body.student_assignment_id,
            )  # fmt: skip
            conv_id = conv["conversation_id"]
        else:
            await execute(
                conn, "UPDATE tutor_conversations SET updated_at = now() WHERE conversation_id = :c", c=conv_id
            )
        await execute(
            conn,
            """INSERT INTO tutor_messages (conversation_id, role, mode, content, input_mode)
               VALUES (:c, 'STUDENT', :mode, :t, :im)""",
            c=conv_id, mode=body.mode, t=body.message, im=body.input_mode,
        )  # fmt: skip
        used = [picked[i]["chunk_id"] for i in reply.used_chunks if i < len(picked)]
        msg = await fetch_one(
            conn,
            """INSERT INTO tutor_messages (conversation_id, role, mode, content, grounded_chunk_ids, off_topic,
                                           created_at)
               VALUES (:c, 'TUTOR', :mode, :t, CAST(:g AS uuid[]), :ot, now() + interval '1 millisecond')
               RETURNING message_id, created_at""",
            c=conv_id, mode=body.mode, t=reply.text, g=[str(u) for u in used], ot=reply.off_topic,
        )  # fmt: skip
    return {
        "data": {"conversation_id": conv_id, "message_id": msg["message_id"], "reply": reply.text,
                 "off_topic": reply.off_topic, "mode": body.mode},
        "meta": {"request_id": request.state.request_id},
    }  # fmt: skip


@router.get("/lessons/{lesson_id}/tutor/conversations")
async def my_conversations(lesson_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    """The student's own recent tutor conversation for this lesson (most recent first, with messages)."""
    sctx = await student_ctx(actor, request)
    async with get_db().connect() as conn:
        convs = await fetch_all(
            conn,
            """SELECT conversation_id, created_at, updated_at FROM tutor_conversations
                WHERE student_id = :s AND lesson_id = :l ORDER BY updated_at DESC LIMIT 5""",
            s=sctx.student_id,
            l=lesson_id,
        )
        for c in convs:
            c["messages"] = await fetch_all(
                conn,
                """SELECT message_id, role, mode, content, input_mode, off_topic, created_at FROM tutor_messages
                    WHERE conversation_id = :c ORDER BY created_at""",
                c=c["conversation_id"],
            )
    return {"data": {"conversations": convs}, "meta": {"request_id": request.state.request_id}}
