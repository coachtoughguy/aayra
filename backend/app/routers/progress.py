"""Student progress (doc 10 MVP: simple and actionable — pending/completed work, mastery/understanding,
upcoming reviews and weekly progress; simple streak without turning it into a game) and the
notifications inbox for every role.

Never shown: rank, percentiles, comparisons with other students, or internal confidence numbers.
Days and weeks use the school's (or personal space's) time zone; weeks start on Monday.
"""

from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.auth import Actor, current_actor, resolve_student_context
from app.db import execute, fetch_all, fetch_one, get_db

router = APIRouter(prefix="/v1", tags=["progress"])

PENDING = ("NOT_STARTED", "IN_PROGRESS", "OVERDUE")
DONE = ("COMPLETED", "SUBMITTED", "SUBMITTED_LATE")


async def activity_days(conn: AsyncConnection, student_id: UUID, tz: str, since: datetime) -> set[date]:
    rows = await fetch_all(
        conn,
        """SELECT DISTINCT (t AT TIME ZONE :tz)::date AS d FROM (
                SELECT coalesce(ended_at, started_at) AS t FROM focus_cycles WHERE student_id = :s AND started_at >= :since
                UNION ALL SELECT completed_at FROM assessment_attempts WHERE student_id = :s AND completed_at >= :since
                UNION ALL SELECT r.answered_at FROM assessment_responses r JOIN assessment_attempts a
                          ON a.attempt_id = r.assessment_attempt_id WHERE a.student_id = :s AND r.answered_at >= :since
           ) x WHERE t IS NOT NULL""",
        s=student_id,
        tz=tz,
        since=since,
    )
    return {r["d"] for r in rows}


def streaks(days: set[date], today: date) -> tuple[int, int]:
    """(current, best). Today still counts as 'not broken yet' until it ends."""
    cur = 0
    d = today if today in days else today - timedelta(days=1)
    while d in days:
        cur += 1
        d -= timedelta(days=1)
    best = run = 0
    prev = None
    for d in sorted(days):
        run = run + 1 if prev and d - prev == timedelta(days=1) else 1
        best = max(best, run)
        prev = d
    return cur, best


async def student_progress(
    conn: AsyncConnection, student_id: UUID, school_id: UUID, *, sources: tuple[str, ...] = ("SCHOOL", "PARENT", "SELF")
) -> dict:
    school = await fetch_one(conn, "SELECT timezone FROM schools WHERE school_id = :s", s=school_id)
    tz = school["timezone"]
    now = datetime.now(ZoneInfo(tz))
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)

    work = await fetch_all(
        conn,
        """SELECT a.source_type, sa.status, sa.is_late, count(*) AS n
             FROM student_assignments sa JOIN assignments a USING (assignment_id)
            WHERE sa.student_id = :s AND a.status IN ('PUBLISHED', 'ACTIVE', 'CLOSED') AND a.source_type = ANY(:src)
            GROUP BY a.source_type, sa.status, sa.is_late""",
        s=student_id,
        src=list(sources),
    )
    by_source: dict[str, dict] = {s: {"pending": 0, "overdue": 0, "completed": 0, "submitted_late": 0} for s in sources}
    for w in work:
        b = by_source[w["source_type"]]
        if w["status"] in PENDING:
            b["pending"] += w["n"]
            b["overdue"] += w["n"] if w["status"] == "OVERDUE" else 0
        elif w["status"] in DONE:
            b["completed"] += w["n"]
            b["submitted_late"] += w["n"] if w["status"] == "SUBMITTED_LATE" or w["is_late"] else 0

    mastery_rows = await fetch_all(
        conn,
        """SELECT sub.subject_id, sub.name AS subject_name, m.mastery_scope, m.mastery_status, count(*) AS n
             FROM student_concept_mastery m JOIN lessons l ON l.lesson_id = m.lesson_id
             JOIN subjects sub ON sub.subject_id = l.subject_id
            WHERE m.student_id = :s AND m.mastery_scope = ANY(:src)
            GROUP BY sub.subject_id, sub.name, m.mastery_scope, m.mastery_status ORDER BY sub.name""",
        s=student_id,
        src=list(sources),
    )
    subjects: dict[tuple, dict] = {}
    for r in mastery_rows:
        key = (r["subject_id"], "SCHOOL" if r["mastery_scope"] == "SCHOOL" else "OWN")
        entry = subjects.setdefault(key, {"subject_id": r["subject_id"], "subject_name": r["subject_name"],
                                          "scope": key[1], "mastered": 0, "developing": 0, "needs_review": 0})  # fmt: skip
        entry[
            {"MASTERED": "mastered", "DEVELOPING": "developing", "NEEDS_REVIEW": "needs_review"}[r["mastery_status"]]
        ] += r["n"]

    reviews = await fetch_all(
        conn,
        """SELECT i.scheduled_for, l.title AS lesson_title, l.lesson_id, count(*) AS concepts
             FROM review_schedule_items i JOIN review_schedules rs USING (review_schedule_id)
             JOIN lessons l ON l.lesson_id = rs.lesson_id
            WHERE rs.student_id = :s AND i.status IN ('UPCOMING', 'DUE', 'OVERDUE') AND i.scheduled_for <= :until
            GROUP BY i.scheduled_for, l.title, l.lesson_id ORDER BY i.scheduled_for, l.title""",
        s=student_id,
        until=today + timedelta(days=7),
    )
    due_today = sum(r["concepts"] for r in reviews if r["scheduled_for"] <= today)

    async def week(start: date) -> dict:
        lo = datetime.combine(start, datetime.min.time(), ZoneInfo(tz))
        hi = lo + timedelta(days=7)
        row = await fetch_one(
            conn,
            """SELECT
                 (SELECT coalesce(sum(actual_seconds), 0) FROM focus_cycles
                   WHERE student_id = :s AND started_at >= :lo AND started_at < :hi) AS focus_seconds,
                 (SELECT count(*) FROM student_lesson_progress
                   WHERE student_id = :s AND finished_learning_at >= :lo AND finished_learning_at < :hi) AS lessons_finished,
                 (SELECT count(*) FROM assessment_attempts WHERE student_id = :s AND review_session_id IS NULL
                     AND status = 'COMPLETED' AND completed_at >= :lo AND completed_at < :hi) AS checks_completed,
                 (SELECT count(*) FROM review_sessions WHERE student_id = :s AND status = 'COMPLETED'
                     AND completed_at >= :lo AND completed_at < :hi) AS reviews_completed""",
            s=student_id,
            lo=lo,
            hi=hi,
        )
        return {"week_start": start, "focus_minutes": round(row["focus_seconds"] / 60), **{k: row[k] for k in
                ("lessons_finished", "checks_completed", "reviews_completed")}}  # fmt: skip

    this_week, last_week = await week(week_start), await week(last_week_start)
    days = await activity_days(
        conn, student_id, tz, datetime.combine(today - timedelta(days=120), datetime.min.time(), ZoneInfo(tz))
    )
    current, best = streaks(days, today)
    week_days = [(week_start + timedelta(days=i)) in days for i in range(7)]
    return {
        "today": today,
        "work": by_source,
        "mastery": list(subjects.values()),
        "reviews": {"due_today": due_today, "next_7_days": reviews},
        "weekly": {"this_week": this_week, "last_week": last_week},
        "streak": {"current_days": current, "best_days": best, "active_today": today in days, "this_week": week_days},
    }


@router.get("/students/me/progress")
async def my_progress(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        sctx = await resolve_student_context(conn, actor)
        data = await student_progress(conn, sctx.student_id, sctx.school_id)
    request.state.school_id = str(sctx.school_id)
    return {"data": data, "meta": {"request_id": request.state.request_id}}


# ------------------------------------------------------------------------------------------------
# Notifications inbox (any role)
# ------------------------------------------------------------------------------------------------
@router.get("/me/notifications")
async def my_notifications(
    request: Request,
    actor: Actor = Depends(current_actor),
    status: str = Query("ALL", pattern="^(ALL|UNREAD)$"),
    limit: int = Query(30, ge=1, le=100),
    before: datetime | None = None,
) -> dict:
    async with get_db().connect() as conn:
        rows = await fetch_all(
            conn,
            f"""SELECT notification_id, school_id, notification_type, title, body, priority, entity_type, entity_id,
                       deep_link, status, read_at, created_at
                  FROM notifications
                 WHERE recipient_user_id = :u AND status <> 'ARCHIVED'
                   AND (expires_at IS NULL OR expires_at > now())
                   {"AND status = 'UNREAD'" if status == "UNREAD" else ""}
                   AND (CAST(:before AS timestamptz) IS NULL OR created_at < :before)
                 ORDER BY created_at DESC LIMIT :lim""",
            u=actor.user_id,
            before=before,
            lim=limit,
        )
        unread = await fetch_one(
            conn,
            """SELECT count(*) AS n FROM notifications WHERE recipient_user_id = :u AND status = 'UNREAD'
                  AND (expires_at IS NULL OR expires_at > now())""",
            u=actor.user_id,
        )
    return {
        "data": {"notifications": rows, "unread_count": unread["n"]},
        "meta": {"request_id": request.state.request_id},
    }


@router.post("/me/notifications/{notification_id}/read")
async def mark_read(notification_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().begin() as conn:
        n = await execute(
            conn,
            """UPDATE notifications SET status = 'READ', read_at = coalesce(read_at, now())
                WHERE notification_id = :n AND recipient_user_id = :u AND status <> 'ARCHIVED'""",
            n=notification_id,
            u=actor.user_id,
        )
    if not n:
        raise errors.not_found("notification", notification_id=str(notification_id))
    return {
        "data": {"notification_id": notification_id, "status": "READ"},
        "meta": {"request_id": request.state.request_id},
    }


@router.post("/me/notifications/read-all")
async def mark_all_read(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().begin() as conn:
        n = await execute(
            conn,
            "UPDATE notifications SET status = 'READ', read_at = now() WHERE recipient_user_id = :u AND status = 'UNREAD'",
            u=actor.user_id,
        )
    return {"data": {"marked_read": n}, "meta": {"request_id": request.state.request_id}}
