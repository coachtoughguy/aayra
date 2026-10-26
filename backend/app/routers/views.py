"""Read-only screen views for the web app's role homes: teacher home and lesson list, the parent's
child overview, and the creator's own parent/self assignment lists.

Privacy rules applied here (contract §4): parents see school work and the work they assigned, never
the child's self-chosen learning or private tutor conversations; nothing shows rank or comparisons.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.auth import Actor, current_actor
from app.db import fetch_all, fetch_one, get_db
from app.routers.family_learning import require_guardian_of
from app.routers.learning import student_ctx
from app.routers.progress import student_progress
from app.services.teaching import require_teacher

router = APIRouter(prefix="/v1", tags=["views"])


def _ok(request: Request, data) -> dict:
    return {"data": data, "meta": {"request_id": request.state.request_id}}


@router.get("/teacher/home")
async def teacher_home(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        teacher = await require_teacher(conn, actor)
        request.state.school_id = str(teacher.school_id)
        teaching = await fetch_all(
            conn,
            """SELECT t.assignment_role, t.section_id, sec.name AS section_name, t.subject_id, sub.name AS subject_name,
                      t.academic_year_id,
                      (SELECT count(*) FROM student_enrollments se WHERE se.section_id = t.section_id
                          AND se.status = 'ACTIVE') AS students
                 FROM teacher_section_subject_assignments t JOIN sections sec USING (section_id)
                 LEFT JOIN subjects sub ON sub.subject_id = t.subject_id
                WHERE t.teacher_id = :t AND t.status = 'ACTIVE' ORDER BY sec.name, sub.name""",
            t=teacher.teacher_id,
        )
        subject_ids = list({t["subject_id"] for t in teaching if t["subject_id"]})
        lessons = (
            await fetch_all(
                conn,
                """SELECT l.lesson_id, l.title, l.status, l.version, l.subject_id, sub.name AS subject_name, l.updated_at,
                      (SELECT count(*) FROM assignments a WHERE a.lesson_id = l.lesson_id AND a.source_type = 'SCHOOL')
                        AS assignments
                 FROM lessons l JOIN subjects sub ON sub.subject_id = l.subject_id
                WHERE l.school_id = :s AND l.subject_id = ANY(:subs)
                  AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.lesson_id = l.lesson_id
                                   AND a.source_type IN ('PARENT', 'SELF'))
                ORDER BY l.updated_at DESC LIMIT 100""",
                s=teacher.school_id,
                subs=subject_ids,
            )
            if subject_ids
            else []
        )
        reviews = await fetch_one(
            conn, "SELECT count(*) AS n FROM teacher_review_items WHERE teacher_id = :t AND status = 'PENDING'",
            t=teacher.teacher_id,
        )  # fmt: skip
        unread = await fetch_one(
            conn,
            "SELECT count(*) AS n FROM notifications WHERE recipient_user_id = :u AND status = 'UNREAD'",
            u=actor.user_id,
        )
        school = await fetch_one(conn, "SELECT name FROM schools WHERE school_id = :s", s=teacher.school_id)
        year = await fetch_one(
            conn, "SELECT academic_year_id, name FROM academic_years WHERE school_id = :s AND status = 'ACTIVE' LIMIT 1",
            s=teacher.school_id,
        )  # fmt: skip
    return _ok(request, {
        "teacher_id": teacher.teacher_id, "school_id": teacher.school_id, "school_name": school["name"],
        "active_year": year, "teaching": teaching, "lessons": lessons,
        "needs_attention": reviews["n"], "unread_notifications": unread["n"],
    })  # fmt: skip


@router.get("/teacher/assignments")
async def teacher_assignments(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    """Published school assignments for the sections/subjects this teacher teaches, newest first."""
    async with get_db().connect() as conn:
        teacher = await require_teacher(conn, actor)
        rows = await fetch_all(
            conn,
            """SELECT a.assignment_id, a.title, a.status, a.due_at, a.completion_mode, a.max_marks, l.lesson_id,
                      sec.name AS section_name, sub.name AS subject_name,
                      (SELECT count(*) FROM student_assignments sa WHERE sa.assignment_id = a.assignment_id) AS students,
                      (SELECT count(*) FROM student_assignments sa WHERE sa.assignment_id = a.assignment_id
                          AND sa.status IN ('COMPLETED', 'SUBMITTED', 'SUBMITTED_LATE')) AS done
                 FROM assignments a JOIN lessons l USING (lesson_id) JOIN subjects sub ON sub.subject_id = l.subject_id
                 JOIN assignment_targets t ON t.assignment_id = a.assignment_id
                 LEFT JOIN sections sec ON sec.section_id = t.section_id
                WHERE a.source_type = 'SCHOOL' AND a.school_id = :s
                  AND EXISTS (SELECT 1 FROM teacher_section_subject_assignments x WHERE x.teacher_id = :t
                                 AND x.status = 'ACTIVE' AND x.subject_id = l.subject_id)
                ORDER BY a.created_at DESC LIMIT 100""",
            s=teacher.school_id,
            t=teacher.teacher_id,
        )
    return _ok(request, {"assignments": rows})


@router.get("/guardians/me/children/{student_id}/overview")
async def child_overview(student_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        child = await require_guardian_of(conn, actor, student_id)
        request.state.school_id = str(child["school_id"])
        progress = await student_progress(conn, student_id, child["school_id"], sources=("SCHOOL", "PARENT"))
        work = await fetch_all(
            conn,
            """SELECT sa.student_assignment_id, a.assignment_id, a.title, a.source_type, sa.status, sa.is_late,
                      sa.effective_due_at AS due_at, sa.completed_at, sa.submitted_at, sub.name AS subject_name,
                      a.created_by = :u AS assigned_by_me
                 FROM student_assignments sa JOIN assignments a USING (assignment_id)
                 JOIN lessons l ON l.lesson_id = a.lesson_id JOIN subjects sub ON sub.subject_id = l.subject_id
                WHERE sa.student_id = :s AND a.source_type IN ('SCHOOL', 'PARENT')
                  AND a.status IN ('PUBLISHED', 'ACTIVE', 'CLOSED')
                ORDER BY coalesce(sa.effective_due_at, a.created_at) DESC LIMIT 100""",
            s=student_id,
            u=actor.user_id,
        )
        for w in work:  # doc 21: "Submitted Late" is shown to student, teacher and parent
            w["label"] = ("SUBMITTED_LATE" if w["status"] == "SUBMITTED_LATE" or (w["is_late"] and w["status"] in
                          ("SUBMITTED", "COMPLETED")) else w["status"])  # fmt: skip
        mine = await fetch_all(
            conn,
            """SELECT a.assignment_id, a.title, a.status, a.review_mode, a.due_at, l.status AS lesson_status, a.created_at
                 FROM assignments a JOIN lessons l USING (lesson_id) JOIN assignment_targets t USING (assignment_id)
                WHERE a.source_type = 'PARENT' AND a.created_by = :u AND t.student_id = :s ORDER BY a.created_at DESC""",
            u=actor.user_id,
            s=student_id,
        )
    for m in mine:
        m["state"] = (
            "ASSIGNED"
            if m["status"] != "DRAFT"
            else {"READY": "READY_TO_ASSIGN", "PROCESSING_FAILED": "FAILED"}.get(m["lesson_status"], "PREPARING")
        )
    return _ok(request, {"child": {k: child[k] for k in ("student_id", "display_name", "school_id")},
                         "progress": progress, "work": work, "my_assignments": mine})  # fmt: skip


@router.get("/self-assignments")
async def my_self_assignments(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    sctx = await student_ctx(actor, request)
    async with get_db().connect() as conn:
        rows = await fetch_all(
            conn,
            """SELECT a.assignment_id, a.lesson_id, a.title, a.status, a.review_mode, a.due_at, a.version,
                      l.status AS lesson_status, sub.name AS subject_name, a.created_at
                 FROM assignments a JOIN lessons l USING (lesson_id) JOIN subjects sub ON sub.subject_id = l.subject_id
                 JOIN assignment_targets t USING (assignment_id)
                WHERE a.source_type = 'SELF' AND a.created_by = :u AND t.student_id = :s ORDER BY a.created_at DESC""",
            u=actor.user_id,
            s=sctx.student_id,
        )
    for r in rows:
        r["state"] = (
            "ASSIGNED"
            if r["status"] != "DRAFT"
            else {"READY": "READY_TO_ASSIGN", "PROCESSING_FAILED": "FAILED"}.get(r["lesson_status"], "PREPARING")
        )
    return _ok(request, {"assignments": rows})


@router.get("/guardians/me/children/{student_id}/subjects")
async def child_subjects(student_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        child = await require_guardian_of(conn, actor, student_id)
        subjects = await fetch_all(
            conn,
            """SELECT s.subject_id, s.code, s.name FROM grade_subjects gs JOIN subjects s USING (subject_id)
                WHERE gs.grade_id = :g ORDER BY s.name""",
            g=child["grade_id"],
        )
    return _ok(request, {"subjects": subjects})
