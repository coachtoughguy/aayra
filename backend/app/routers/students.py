"""Contract §6.5 — Student Home (ST-01) and My Learning (ST-02).

Home answers "what should I do now?" (doc 21 v0.6/v0.7):
  - Continue Learning: one already-started, unfinished item; most recently active wins.
  - What's Next: deterministic tiers Overdue -> Due Today -> Review Due Today -> Due Tomorrow ->
    later; earlier due wins within a tier. "Today" is the school's local day.
No ranking or peer comparison appears anywhere.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from app.auth import Actor, current_actor, resolve_student_context
from app.db import fetch_all, fetch_one, get_db

router = APIRouter(prefix="/v1/students/me", tags=["student"])

DONE = ("COMPLETED", "SUBMITTED", "SUBMITTED_LATE")
TIER_ORDER = ["OVERDUE", "DUE_TODAY", "REVIEW_DUE_TODAY", "DUE_TOMORROW", "LATER"]


async def _visible_work(conn: AsyncConnection, student_id) -> list[dict[str, Any]]:
    """Everything assigned to the student that is released (scheduled work stays hidden)."""
    return await fetch_all(
        conn,
        """SELECT sa.student_assignment_id, sa.assignment_id, a.lesson_id, a.title, a.source_type AS source,
                  sa.status, coalesce(sa.effective_due_at, a.due_at) AS due_at, sa.is_late, sa.started_at,
                  sa.completed_at, sub.subject_id, sub.name AS subject_name, slp.last_activity_at
             FROM student_assignments sa
             JOIN assignments a ON a.assignment_id = sa.assignment_id
             JOIN lessons l ON l.lesson_id = a.lesson_id
             JOIN subjects sub ON sub.subject_id = l.subject_id
             LEFT JOIN student_lesson_progress slp ON slp.student_assignment_id = sa.student_assignment_id
            WHERE sa.student_id = :st
              AND a.status IN ('PUBLISHED', 'ACTIVE', 'CLOSED')
              AND (a.available_from IS NULL OR a.available_from <= now())""",
        st=student_id,
    )


def _item(row: dict[str, Any], tier: str | None = None) -> dict[str, Any]:
    out = {
        "student_assignment_id": row["student_assignment_id"],
        "assignment_id": row["assignment_id"],
        "lesson_id": row["lesson_id"],
        "title": row["title"],
        "subject": {"subject_id": row["subject_id"], "name": row["subject_name"]},
        "source": row["source"],
        "status": row["status"],
        "due_at": row["due_at"],
        "is_late": row["is_late"],
    }
    if tier:
        out["tier"] = tier
    return out


def _tier(due_at: datetime | None, now: datetime, tz: ZoneInfo) -> str:
    if due_at is None:
        return "LATER"
    if due_at < now:
        return "OVERDUE"
    today = now.astimezone(tz).date()
    due_day = due_at.astimezone(tz).date()
    if due_day == today:
        return "DUE_TODAY"
    if due_day == today + timedelta(days=1):
        return "DUE_TOMORROW"
    return "LATER"


@router.get("/home")
async def home(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    now = datetime.now(UTC)
    async with get_db().connect() as conn:
        ctx = await resolve_student_context(conn, actor)
        request.state.school_id = str(ctx.school_id)
        school = await fetch_one(conn, "SELECT timezone FROM schools WHERE school_id = :s", s=ctx.school_id)
        work = await _visible_work(conn, ctx.student_id)
    tz = ZoneInfo(school["timezone"])
    pending = [w for w in work if w["status"] not in DONE]

    started = [w for w in pending if w["status"] == "IN_PROGRESS"]
    started.sort(
        key=lambda w: w["last_activity_at"] or w["started_at"] or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    continue_learning = _item(started[0]) if started else None

    far_future = datetime.max.replace(tzinfo=UTC)
    queue = [
        (TIER_ORDER.index(t := _tier(w["due_at"], now, tz)), w["due_at"] or far_future, w["title"], w, t)
        for w in pending
        if not continue_learning or w["student_assignment_id"] != continue_learning["student_assignment_id"]
    ]
    queue.sort(key=lambda q: q[:3])
    week_start = (now.astimezone(tz) - timedelta(days=now.astimezone(tz).weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    done = [w for w in work if w["status"] in DONE]
    return {
        "data": {
            "continue_learning": continue_learning,
            "whats_next": [_item(w, t) for *_, w, t in queue],
            "my_learning": {"pending_count": len(pending)},
            "my_reviews": {"due_today_count": 0},  # spaced review arrives with Slice 2 (§7.6)
            "progress_snapshot": {
                "assigned_total": len(work),
                "completed_total": len(done),
                "completed_this_week": sum(1 for w in done if w["completed_at"] and w["completed_at"] >= week_start),
            },
        },
        "meta": {"request_id": request.state.request_id},
    }


@router.get("/learning")
async def my_learning(request: Request, include_completed: bool = False, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        ctx = await resolve_student_context(conn, actor)
        request.state.school_id = str(ctx.school_id)
        subjects = await fetch_all(
            conn,
            """SELECT s.subject_id, s.name, s.code FROM grade_subjects gs JOIN subjects s USING (subject_id)
                WHERE gs.grade_id = :g ORDER BY s.name""",
            g=ctx.grade_id,
        )
        work = await _visible_work(conn, ctx.student_id)
    by_subject: dict = {s["subject_id"]: {**s, "pending": [], "completed": [], "completed_count": 0} for s in subjects}
    for w in sorted(work, key=lambda w: (w["due_at"] is None, w["due_at"] or datetime.max.replace(tzinfo=UTC))):
        bucket = by_subject.setdefault(
            w["subject_id"],
            {"subject_id": w["subject_id"], "name": w["subject_name"], "code": None,
             "pending": [], "completed": [], "completed_count": 0},
        )  # fmt: skip
        if w["status"] in DONE:
            bucket["completed_count"] += 1
            if include_completed:
                bucket["completed"].append(_item(w))
        else:
            bucket["pending"].append(_item(w))
    if not include_completed:
        for b in by_subject.values():
            b.pop("completed")
    return {"data": {"subjects": list(by_subject.values())}, "meta": {"request_id": request.state.request_id}}
