"""ContentIntelligenceService (contract §6.2): attached content -> evidence chunks -> concepts ->
a new assessment_blueprints version with generated questions -> lesson READY.

Per-item work is a `content_processing_jobs` EXTRACTION job. Per-lesson work is a
`background_jobs` LESSON_PREPARATION job, enqueued whenever an attached item finishes; it only
does real work once every attached item has evidence. The stage topology lives here and can
change without touching the API contract.
"""

import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from app.ai.content_ai import ContentExtractionError, Usage, get_content_ai
from app.context import RequestContext
from app.db import Database, execute, fetch_all, fetch_one
from app.outbox import emit

MAX_CONTENT_JOB_ATTEMPTS = 3


def system_context(job_id: UUID) -> RequestContext:
    return RequestContext(request_id=job_id, correlation_id=job_id, operation="SYSTEM worker")


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", name.lower())).strip()


# --- enqueueing ----------------------------------------------------------------------------------
async def enqueue_item_processing(conn: AsyncConnection, school_id: UUID, content_item_id: UUID) -> None:
    await execute(
        conn,
        """INSERT INTO content_processing_jobs (school_id, content_item_id, job_type, status)
           VALUES (:s, :c, 'EXTRACTION', 'QUEUED')""",
        s=school_id,
        c=content_item_id,
    )


async def item_extraction_state(conn: AsyncConnection, content_item_id: UUID) -> str | None:
    """Latest EXTRACTION job status for the item (None = never queued)."""
    row = await fetch_one(
        conn,
        """SELECT status FROM content_processing_jobs
            WHERE content_item_id = :c AND job_type = 'EXTRACTION'
            ORDER BY created_at DESC LIMIT 1""",
        c=content_item_id,
    )
    return row["status"] if row else None


async def enqueue_lesson_preparation(conn: AsyncConnection, school_id: UUID, lesson_id: UUID) -> None:
    pending = await fetch_one(
        conn,
        """SELECT 1 AS x FROM background_jobs
            WHERE job_type = 'LESSON_PREPARATION' AND entity_id = :l AND status IN ('QUEUED', 'RETRYING')""",
        l=lesson_id,
    )
    if pending is None:
        await execute(
            conn,
            """INSERT INTO background_jobs (school_id, job_type, entity_type, entity_id, status, max_attempts)
               VALUES (:s, 'LESSON_PREPARATION', 'lesson', :l, 'QUEUED', 3)""",
            s=school_id,
            l=lesson_id,
        )


# --- ai_invocations (contract §12: mandatory for every model call) -------------------------------
async def log_invocation(
    conn: AsyncConnection,
    *,
    school_id: UUID,
    purpose: str,
    entity_type: str,
    entity_id: UUID,
    usage: Usage | None,
    latency_ms: int,
    status: str,
) -> None:
    ai = get_content_ai()
    await execute(
        conn,
        """INSERT INTO ai_invocations (school_id, purpose, entity_type, entity_id, model_name, prompt_version,
                                       input_token_count, output_token_count, latency_ms, status)
           VALUES (:s, :p, :et, :eid, :m, :pv, :it, :ot, :lat, :st)""",
        s=school_id,
        p=purpose,
        et=entity_type,
        eid=entity_id,
        m=ai.model_name,
        pv=ai.prompt_version,
        it=usage.input_tokens if usage else None,
        ot=usage.output_tokens if usage else None,
        lat=latency_ms,
        st=status,
    )


# --- stage 1: per-item extraction ----------------------------------------------------------------
async def run_extraction_job(db: Database, job: dict[str, Any]) -> None:
    ctx = system_context(job["job_id"])
    async with db.begin() as conn:
        item = await fetch_one(conn, "SELECT * FROM content_items WHERE content_item_id = :c", c=job["content_item_id"])
        assert item is not None
        started = time.perf_counter()
        try:
            chunks, usage = await get_content_ai().extract(item)
        except ContentExtractionError as exc:
            await log_invocation(
                conn,
                school_id=job["school_id"],
                purpose="CONTENT_EXTRACTION",
                entity_type="content_item",
                entity_id=item["content_item_id"],
                usage=None,
                latency_ms=int((time.perf_counter() - started) * 1000),
                status="FAILED",
            )
            await execute(
                conn,
                """UPDATE content_processing_jobs SET status = 'FAILED', completed_at = now(),
                          error_code = :code, error_message = :msg WHERE job_id = :j""",
                j=job["job_id"],
                code=exc.code,
                msg=str(exc),
            )
            await _fail_lessons_using(conn, ctx, item, exc.code, job["job_id"])
            return
        await log_invocation(
            conn,
            school_id=job["school_id"],
            purpose="CONTENT_EXTRACTION",
            entity_type="content_item",
            entity_id=item["content_item_id"],
            usage=usage,
            latency_ms=int((time.perf_counter() - started) * 1000),
            status="SUCCEEDED",
        )
        for ch in chunks:
            await execute(
                conn,
                """INSERT INTO content_evidence_chunks (school_id, content_item_id, chunk_type, content_text,
                                                        page_number, start_seconds, end_seconds, extraction_method)
                   VALUES (:s, :c, :t, :txt, :p, :ss, :es, :m)""",
                s=job["school_id"],
                c=item["content_item_id"],
                t=ch.chunk_type,
                txt=ch.text,
                p=ch.page_number,
                ss=ch.start_seconds,
                es=ch.end_seconds,
                m=get_content_ai().model_name,
            )
        await execute(
            conn,
            "UPDATE content_processing_jobs SET status = 'SUCCEEDED', completed_at = now() WHERE job_id = :j",
            j=job["job_id"],
        )
        await emit(
            conn,
            ctx,
            event_type="ContentProcessingStageCompleted",
            aggregate_type="content_item",
            aggregate_id=item["content_item_id"],
            school_id=job["school_id"],
            data={"content_item_id": item["content_item_id"], "stage": "EXTRACTION", "chunks": len(chunks)},
        )
        for lesson in await _processing_lessons_using(conn, item["content_item_id"]):
            await enqueue_lesson_preparation(conn, lesson["school_id"], lesson["lesson_id"])


async def _processing_lessons_using(conn: AsyncConnection, content_item_id: UUID) -> list[dict]:
    return await fetch_all(
        conn,
        """SELECT l.lesson_id, l.school_id FROM lessons l JOIN lesson_content lc USING (lesson_id)
            WHERE lc.content_item_id = :c AND l.status = 'PROCESSING'""",
        c=content_item_id,
    )


async def _fail_lessons_using(conn: AsyncConnection, ctx: RequestContext, item: dict, code: str, job_id: UUID) -> None:
    for lesson in await _processing_lessons_using(conn, item["content_item_id"]):
        await execute(
            conn,
            "UPDATE lessons SET status = 'PROCESSING_FAILED', version = version + 1 WHERE lesson_id = :l",
            l=lesson["lesson_id"],
        )
        await emit(
            conn,
            ctx,
            event_type="ContentProcessingFailed",
            aggregate_type="lesson",
            aggregate_id=lesson["lesson_id"],
            school_id=lesson["school_id"],
            data={"job_id": job_id, "content_item_id": item["content_item_id"], "error_code": code},
        )


# --- stage 2: per-lesson concept extraction + assessment preparation ------------------------------
async def run_lesson_preparation(db: Database, job: dict[str, Any]) -> None:
    ctx = system_context(job["job_id"])
    async with db.begin() as conn:
        lesson = await fetch_one(conn, "SELECT * FROM lessons WHERE lesson_id = :l FOR UPDATE", l=job["entity_id"])
        if lesson is None or lesson["status"] != "PROCESSING":
            return  # superseded (failed, or already prepared by an earlier job)
        items = await fetch_all(
            conn,
            "SELECT content_item_id FROM lesson_content WHERE lesson_id = :l ORDER BY display_order",
            l=lesson["lesson_id"],
        )
        states = [await item_extraction_state(conn, i["content_item_id"]) for i in items]
        failed = [i["content_item_id"] for i, s in zip(items, states, strict=True) if s in ("FAILED", "CANCELLED")]
        if failed:
            # A file that can't be read blocks the lesson until the teacher retries or removes it.
            await execute(
                conn,
                "UPDATE lessons SET status = 'PROCESSING_FAILED', version = version + 1 WHERE lesson_id = :l",
                l=lesson["lesson_id"],
            )
            await emit(
                conn,
                ctx,
                event_type="ContentProcessingFailed",
                aggregate_type="lesson",
                aggregate_id=lesson["lesson_id"],
                school_id=lesson["school_id"],
                data={"job_id": job["job_id"], "content_item_ids": failed, "error_code": "CONTENT_ITEM_FAILED"},
            )
            return
        if not items or any(s != "SUCCEEDED" for s in states):
            return  # another item is still processing; its completion re-enqueues this job
        chunks = await fetch_all(
            conn,
            """SELECT ch.chunk_id, ch.content_text FROM content_evidence_chunks ch
                 JOIN lesson_content lc ON lc.content_item_id = ch.content_item_id
                WHERE lc.lesson_id = :l ORDER BY lc.display_order, ch.page_number NULLS LAST, ch.created_at""",
            l=lesson["lesson_id"],
        )
        await _prepare_assessment(conn, ctx, lesson, chunks)


async def _prepare_assessment(conn: AsyncConnection, ctx: RequestContext, lesson: dict, chunks: list[dict]) -> None:
    ai = get_content_ai()
    lesson_id, school_id = lesson["lesson_id"], lesson["school_id"]
    run = await fetch_one(
        conn,
        """INSERT INTO ai_generation_runs (school_id, run_type, entity_type, entity_id, model_name, prompt_version,
                                           status, input_summary, started_at)
           VALUES (:s, 'ASSESSMENT_PREPARATION', 'lesson', :l, :m, :pv, 'RUNNING', CAST(:inp AS jsonb), now())
           RETURNING generation_run_id""",
        s=school_id,
        l=lesson_id,
        m=ai.model_name,
        pv=ai.prompt_version,
        inp=json.dumps({"chunks": len(chunks)}),
    )
    run_id = run["generation_run_id"]

    t0 = time.perf_counter()
    concepts, u1 = await ai.extract_concepts(lesson, [c["content_text"] for c in chunks])
    await log_invocation(
        conn,
        school_id=school_id,
        purpose="CONCEPT_EXTRACTION",
        entity_type="lesson",
        entity_id=lesson_id,
        usage=u1,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        status="SUCCEEDED",
    )
    t1 = time.perf_counter()
    questions, u2 = await ai.generate_questions(lesson, concepts)
    await log_invocation(
        conn,
        school_id=school_id,
        purpose="ASSESSMENT_PREPARATION",
        entity_type="lesson",
        entity_id=lesson_id,
        usage=u2,
        latency_ms=int((time.perf_counter() - t1) * 1000),
        status="SUCCEEDED",
    )

    # Concepts are reused per subject by normalized name; lesson_concepts is the lesson's current scope.
    concept_ids: dict[str, UUID] = {}
    await execute(conn, "DELETE FROM lesson_concepts WHERE lesson_id = :l", l=lesson_id)
    for order, c in enumerate(concepts):
        norm = _normalize(c.name)
        existing = await fetch_one(
            conn,
            "SELECT concept_id FROM concepts WHERE subject_id = :s AND normalized_name = :n AND school_id = :sc",
            s=lesson["subject_id"],
            n=norm,
            sc=school_id,
        )
        cid = existing["concept_id"] if existing else uuid4()
        if not existing:
            await execute(
                conn,
                """INSERT INTO concepts (concept_id, school_id, subject_id, name, normalized_name, description)
                   VALUES (:id, :sc, :s, :n, :nn, :d)""",
                id=cid,
                sc=school_id,
                s=lesson["subject_id"],
                n=c.name,
                nn=norm,
                d=c.description,
            )
        concept_ids[c.name] = cid
        await execute(
            conn,
            """INSERT INTO lesson_concepts (lesson_id, concept_id, importance, is_explicitly_taught, display_order)
               VALUES (:l, :c, :imp, true, :o)""",
            l=lesson_id,
            c=cid,
            imp=c.importance,
            o=order,
        )
        for idx in c.evidence_chunk_indexes:
            if idx < len(chunks):
                await execute(
                    conn,
                    """INSERT INTO concept_evidence_sources (concept_id, chunk_id, relevance_score)
                       SELECT :c, :ch, 1.0 WHERE NOT EXISTS
                         (SELECT 1 FROM concept_evidence_sources WHERE concept_id = :c AND chunk_id = :ch)""",
                    c=cid,
                    ch=chunks[idx]["chunk_id"],
                )

    # New immutable generation: blueprint version N+1; older versions are superseded, never edited.
    prev = await fetch_one(
        conn, "SELECT coalesce(max(version), 0) AS v FROM assessment_blueprints WHERE lesson_id = :l", l=lesson_id
    )
    version = prev["v"] + 1
    await execute(
        conn,
        "UPDATE assessment_blueprints SET status = 'SUPERSEDED' WHERE lesson_id = :l AND status = 'ACTIVE'",
        l=lesson_id,
    )
    bp = await fetch_one(
        conn,
        """INSERT INTO assessment_blueprints (lesson_id, generation_run_id, version, status, generated_at)
           VALUES (:l, :r, :v, 'ACTIVE', now()) RETURNING blueprint_id""",
        l=lesson_id,
        r=run_id,
        v=version,
    )
    blueprint_id = bp["blueprint_id"]
    for priority, c in enumerate(concepts, start=1):
        kinds = sorted({q.cognitive_type for q in questions if q.concept_name == c.name})
        await execute(
            conn,
            """INSERT INTO assessment_blueprint_concepts (blueprint_id, concept_id, required_evidence, priority)
               VALUES (:b, :c, CAST(:req AS jsonb), :p)""",
            b=blueprint_id,
            c=concept_ids[c.name],
            req=json.dumps({"min_correct": 1 if c.importance != "CORE" else 2, "cognitive_types": kinds}),
            p=priority,
        )
    for q in questions:
        await execute(
            conn,
            """INSERT INTO questions (lesson_id, concept_id, blueprint_id, question_type, cognitive_type, prompt_text,
                                      difficulty, answer_spec, generated_by_model, source_provenance)
               VALUES (:l, :c, :b, :qt, :ct, :p, :d, CAST(:ans AS jsonb), :m, CAST(:prov AS jsonb))""",
            l=lesson_id,
            c=concept_ids[q.concept_name],
            b=blueprint_id,
            qt=q.question_type,
            ct=q.cognitive_type,
            p=q.prompt_text,
            d=q.difficulty,
            ans=json.dumps(q.answer_spec),
            m=ai.model_name,
            prov=json.dumps({"generation_run_id": str(run_id), "blueprint_version": version}),
        )
    await execute(
        conn,
        """UPDATE ai_generation_runs SET status = 'SUCCEEDED', completed_at = now(),
                  output_summary = CAST(:out AS jsonb) WHERE generation_run_id = :r""",
        r=run_id,
        out=json.dumps({"concepts": len(concepts), "questions": len(questions), "blueprint_version": version}),
    )
    await execute(
        conn,
        "UPDATE lessons SET status = 'READY', version = version + 1 WHERE lesson_id = :l",
        l=lesson_id,
    )
    await emit(
        conn,
        ctx,
        event_type="LessonReadyForReview",
        aggregate_type="lesson",
        aggregate_id=lesson_id,
        school_id=school_id,
        data={"lesson_id": lesson_id, "generation_version": version, "at": datetime.now(UTC)},
    )
