"""Assignment fan-out (contract §6.4 async follow-up) and scheduled release.

The `background_jobs` ASSIGNMENT_FANOUT row is the authoritative distribution status. Students are
delivered in committed batches; the upsert is unique on (assignment_id, student_id), so a crashed
run can simply be re-run and only creates the missing rows.
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import Database, execute, fetch_all, fetch_one
from app.outbox import emit
from app.services.pipeline import system_context

BATCH_SIZE = 10
# Test hook: called with the batch index after each committed batch (fault injection, §14).
after_batch_hook: Callable[[int], None] | None = None

STATUS_MAP = {"QUEUED": "PENDING", "PROCESSING": "PROCESSING", "COMPLETED": "COMPLETE"}


async def resolve_roster(conn: AsyncConnection, assignment_id: UUID) -> list[dict[str, Any]]:
    """Current ACTIVE students for the assignment's target (resolved now, never a stored list)."""
    return await fetch_all(
        conn,
        """SELECT DISTINCT se.student_id, sp.user_id
             FROM assignment_targets t
             JOIN assignments a ON a.assignment_id = t.assignment_id
             JOIN lessons l ON l.lesson_id = a.lesson_id
             JOIN student_enrollments se
               ON se.status = 'ACTIVE' AND se.academic_year_id = l.academic_year_id
              AND ((t.target_type = 'SECTION' AND se.section_id = t.section_id)
                OR (t.target_type = 'STUDENT' AND se.student_id = t.student_id))
             JOIN student_profiles sp ON sp.student_id = se.student_id AND sp.status = 'ACTIVE'
            WHERE t.assignment_id = :a
            ORDER BY se.student_id""",
        a=assignment_id,
    )


async def _notify(conn: AsyncConnection, assignment: dict, rows: list[dict]) -> None:
    for r in rows:
        await execute(
            conn,
            """INSERT INTO notifications (school_id, recipient_user_id, notification_type, title, body, priority,
                                          entity_type, entity_id, deep_link)
               VALUES (:s, :u, 'ASSIGNMENT_PUBLISHED', :t, :b, 'NORMAL', 'student_assignment', :sa, :link)""",
            s=assignment["school_id"],
            u=r["user_id"],
            t=f"New lesson: {assignment['title']}",
            b=f"Due {assignment['due_at']:%a %d %b}" if assignment["due_at"] else None,
            sa=r["student_assignment_id"],
            link=f"aayra://student/assignments/{r['student_assignment_id']}",
        )


async def run_fanout(db: Database, job: dict[str, Any]) -> None:
    ctx = system_context(job["job_id"])
    async with db.connect() as conn:
        assignment = await fetch_one(conn, "SELECT * FROM assignments WHERE assignment_id = :a", a=job["entity_id"])
        assert assignment is not None
        roster = await resolve_roster(conn, assignment["assignment_id"])

    delivered = 0
    for index, start in enumerate(range(0, len(roster), BATCH_SIZE)):
        batch = roster[start : start + BATCH_SIZE]
        async with db.begin() as conn:
            created: list[dict] = []
            for s in batch:
                row = await fetch_one(
                    conn,
                    """INSERT INTO student_assignments (school_id, assignment_id, student_id, effective_due_at)
                       VALUES (:sc, :a, :st, :due)
                       ON CONFLICT (assignment_id, student_id) DO NOTHING
                       RETURNING student_assignment_id""",
                    sc=assignment["school_id"],
                    a=assignment["assignment_id"],
                    st=s["student_id"],
                    due=assignment["due_at"],
                )
                if row is None:
                    continue  # delivered by an earlier (crashed) run
                await execute(
                    conn,
                    """INSERT INTO student_lesson_progress (student_id, lesson_id, student_assignment_id)
                       VALUES (:st, :l, :sa) ON CONFLICT DO NOTHING""",
                    st=s["student_id"],
                    l=assignment["lesson_id"],
                    sa=row["student_assignment_id"],
                )
                created.append({**s, "student_assignment_id": row["student_assignment_id"]})
            if assignment["status"] != "SCHEDULED":
                await _notify(conn, assignment, created)
            delivered += len(created)
        if after_batch_hook:
            after_batch_hook(index)

    async with db.begin() as conn:
        await execute(
            conn,
            """UPDATE background_jobs SET status = 'COMPLETED', completed_at = now(), last_error = NULL
                WHERE job_id = :j""",
            j=job["job_id"],
        )
        if assignment["status"] == "SCHEDULED":
            await execute(
                conn,
                """INSERT INTO background_jobs (school_id, job_type, entity_type, entity_id, status, max_attempts,
                                                scheduled_at)
                   SELECT :s, 'ASSIGNMENT_RELEASE', 'assignment', :a, 'QUEUED', 5, :at
                    WHERE NOT EXISTS (SELECT 1 FROM background_jobs
                                       WHERE job_type = 'ASSIGNMENT_RELEASE' AND entity_id = :a)""",
                s=assignment["school_id"],
                a=assignment["assignment_id"],
                at=assignment["available_from"],
            )
        total = await fetch_one(
            conn,
            "SELECT count(*) AS n FROM student_assignments WHERE assignment_id = :a",
            a=assignment["assignment_id"],
        )
        await emit(
            conn,
            ctx,
            event_type="AssignmentDistributed",
            aggregate_type="assignment",
            aggregate_id=assignment["assignment_id"],
            school_id=assignment["school_id"],
            data={"assignment_id": assignment["assignment_id"], "students": total["n"], "this_run": delivered},
        )


async def run_release(db: Database, job: dict[str, Any]) -> None:
    """Scheduled assignment reaches available_from: becomes visible and students are notified."""
    ctx = system_context(job["job_id"])
    async with db.begin() as conn:
        assignment = await fetch_one(
            conn, "SELECT * FROM assignments WHERE assignment_id = :a FOR UPDATE", a=job["entity_id"]
        )
        if assignment and assignment["status"] == "SCHEDULED":
            await execute(
                conn,
                """UPDATE assignments SET status = 'PUBLISHED', version = version + 1
                    WHERE assignment_id = :a""",
                a=assignment["assignment_id"],
            )
            rows = await fetch_all(
                conn,
                """SELECT sa.student_assignment_id, sp.user_id FROM student_assignments sa
                     JOIN student_profiles sp USING (student_id) WHERE sa.assignment_id = :a""",
                a=assignment["assignment_id"],
            )
            await _notify(conn, assignment, rows)
            await emit(
                conn,
                ctx,
                event_type="AssignmentReleased",
                aggregate_type="assignment",
                aggregate_id=assignment["assignment_id"],
                school_id=assignment["school_id"],
                data={"assignment_id": assignment["assignment_id"], "students": len(rows)},
            )
        await execute(
            conn,
            "UPDATE background_jobs SET status = 'COMPLETED', completed_at = now() WHERE job_id = :j",
            j=job["job_id"],
        )


async def distribution_status(conn: AsyncConnection, assignment_id: UUID) -> dict[str, Any]:
    job = await fetch_one(
        conn,
        """SELECT status, last_error, attempt_count FROM background_jobs
            WHERE job_type = 'ASSIGNMENT_FANOUT' AND entity_id = :a ORDER BY created_at DESC LIMIT 1""",
        a=assignment_id,
    )
    delivered = (
        await fetch_one(conn, "SELECT count(*) AS n FROM student_assignments WHERE assignment_id = :a", a=assignment_id)
    )["n"]
    if job is None:
        status = "PENDING"
    elif job["status"] in ("RETRYING", "FAILED"):
        status = "PARTIAL_FAILURE" if delivered else ("FAILED" if job["status"] == "FAILED" else "PENDING")
    else:
        status = STATUS_MAP.get(job["status"], job["status"])
    expected = len(await resolve_roster(conn, assignment_id))
    return {
        "distribution_status": status,
        "delivered_count": delivered,
        "expected_count": expected,
        "last_error": job["last_error"] if job and status != "COMPLETE" else None,
    }
