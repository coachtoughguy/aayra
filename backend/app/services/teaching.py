"""Teacher authority checks (contract §3).

Authority is resolved from ACTIVE teacher_section_subject_assignments on EVERY call, never cached,
so a teacher moved off a subject loses access immediately while historical attribution stays.
Primary and Co-teachers carry identical permissions (doc 21 v1.0).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.auth import Actor
from app.db import fetch_one

TEACHING_ROLES = ("PRIMARY_SUBJECT_TEACHER", "CO_TEACHER")


@dataclass(frozen=True)
class Teacher:
    teacher_id: UUID
    school_id: UUID
    user_id: UUID


async def require_teacher(conn: AsyncConnection, actor: Actor) -> Teacher:
    row = await fetch_one(
        conn,
        "SELECT teacher_id, school_id FROM teacher_profiles WHERE user_id = :u AND status = 'ACTIVE'",
        u=actor.user_id,
    )
    if row is None or not actor.has_role(row["school_id"], "TEACHER"):
        raise errors.insufficient_role_scope(required=["TEACHER"])
    return Teacher(row["teacher_id"], row["school_id"], actor.user_id)


async def require_subject_authority(
    conn: AsyncConnection, actor: Actor, subject_id: UUID, academic_year_id: UUID
) -> Teacher:
    """Teaches this subject (in any section) in this academic year."""
    teacher = await require_teacher(conn, actor)
    row = await fetch_one(
        conn,
        """SELECT 1 AS ok FROM teacher_section_subject_assignments
            WHERE teacher_id = :t AND subject_id = :s AND academic_year_id = :y
              AND status = 'ACTIVE' AND assignment_role = ANY(:roles)
            LIMIT 1""",
        t=teacher.teacher_id,
        s=subject_id,
        y=academic_year_id,
        roles=list(TEACHING_ROLES),
    )
    if row is None:
        raise errors.insufficient_role_scope(reason="not_teaching_subject", subject_id=str(subject_id))
    return teacher


async def teaches_section_subject(conn: AsyncConnection, teacher_id: UUID, section_id: UUID, subject_id: UUID) -> bool:
    row = await fetch_one(
        conn,
        """SELECT 1 AS ok FROM teacher_section_subject_assignments
            WHERE teacher_id = :t AND section_id = :sec AND subject_id = :s
              AND status = 'ACTIVE' AND assignment_role = ANY(:roles)
            LIMIT 1""",
        t=teacher_id,
        sec=section_id,
        s=subject_id,
        roles=list(TEACHING_ROLES),
    )
    return row is not None


async def load_lesson_for_teacher(
    conn: AsyncConnection, actor: Actor, lesson_id: UUID, *, lock: bool = False
) -> tuple[dict, Teacher]:
    lesson = await fetch_one(
        conn, f"SELECT * FROM lessons WHERE lesson_id = :l{' FOR UPDATE' if lock else ''}", l=lesson_id
    )
    if lesson is None:
        raise errors.not_found("lesson", lesson_id=str(lesson_id))
    teacher = await require_subject_authority(conn, actor, lesson["subject_id"], lesson["academic_year_id"])
    if teacher.school_id != lesson["school_id"]:
        raise errors.insufficient_role_scope()
    return lesson, teacher
