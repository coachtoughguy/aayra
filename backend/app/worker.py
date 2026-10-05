"""Background worker: claims queued jobs with FOR UPDATE SKIP LOCKED and runs them.

    uv run python -m app.worker            # poll forever (local / deployment)
Tests call `await drain(db)` to run everything that is due, deterministically.
"""

import asyncio
import logging
from typing import Any

from app.db import Database, execute, fetch_one, get_db
from app.services import distribution, pipeline

log = logging.getLogger("aayra.worker")

BACKGROUND_HANDLERS = {
    "LESSON_PREPARATION": pipeline.run_lesson_preparation,
    "ASSIGNMENT_FANOUT": distribution.run_fanout,
    "ASSIGNMENT_RELEASE": distribution.run_release,
}


async def _claim_content_job(db: Database) -> dict[str, Any] | None:
    async with db.begin() as conn:
        return await fetch_one(
            conn,
            """UPDATE content_processing_jobs SET status = 'PROCESSING', started_at = now(),
                      attempt_count = attempt_count + 1
                WHERE job_id = (SELECT job_id FROM content_processing_jobs
                                 WHERE status IN ('QUEUED', 'RETRYING')
                                 ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING *""",
        )


async def _claim_background_job(db: Database) -> dict[str, Any] | None:
    async with db.begin() as conn:
        return await fetch_one(
            conn,
            """UPDATE background_jobs SET status = 'PROCESSING', started_at = now(), attempt_count = attempt_count + 1
                WHERE job_id = (SELECT job_id FROM background_jobs
                                 WHERE status IN ('QUEUED', 'RETRYING')
                                   AND (scheduled_at IS NULL OR scheduled_at <= now())
                                 ORDER BY coalesce(scheduled_at, created_at) FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING *""",
        )


async def run_once(db: Database) -> bool:
    """Run at most one job. Returns False when nothing is due."""
    job = await _claim_content_job(db)
    if job is not None:
        try:
            await pipeline.run_extraction_job(db, job)
        except Exception as exc:
            log.exception("content job %s failed", job["job_id"])
            final = job["attempt_count"] >= pipeline.MAX_CONTENT_JOB_ATTEMPTS
            async with db.begin() as conn:
                await execute(
                    conn,
                    """UPDATE content_processing_jobs SET status = :st, error_code = 'INTERNAL',
                              error_message = :msg WHERE job_id = :j""",
                    st="FAILED" if final else "RETRYING",
                    msg=repr(exc)[:2000],
                    j=job["job_id"],
                )
        return True

    job = await _claim_background_job(db)
    if job is None:
        return False
    handler = BACKGROUND_HANDLERS.get(job["job_type"])
    try:
        if handler is None:
            raise RuntimeError(f"no handler for job type {job['job_type']}")
        await handler(db, job)
        async with db.begin() as conn:  # handlers may finish without marking (e.g. nothing to do)
            await execute(
                conn,
                """UPDATE background_jobs SET status = 'COMPLETED', completed_at = now()
                    WHERE job_id = :j AND status = 'PROCESSING'""",
                j=job["job_id"],
            )
    except Exception as exc:
        log.exception("background job %s (%s) failed", job["job_id"], job["job_type"])
        final = job["attempt_count"] >= job["max_attempts"]
        async with db.begin() as conn:
            await execute(
                conn,
                "UPDATE background_jobs SET status = :st, last_error = :msg WHERE job_id = :j",
                st="FAILED" if final else "RETRYING",
                msg=repr(exc)[:2000],
                j=job["job_id"],
            )
    return True


async def drain(db: Database | None = None, *, max_jobs: int = 10_000) -> int:
    db = db or get_db()
    ran = 0
    while ran < max_jobs and await run_once(db):
        ran += 1
    return ran


async def main(poll_seconds: float = 1.0) -> None:  # pragma: no cover - process entrypoint
    logging.basicConfig(level=logging.INFO)
    db = get_db()
    while True:
        if not await run_once(db):
            await asyncio.sleep(poll_seconds)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
