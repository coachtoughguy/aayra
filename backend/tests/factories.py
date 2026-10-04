"""Small data builders and token minting for tests."""

import time
import uuid
from uuid import UUID

import jwt
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import get_settings
from app.db import execute, fetch_one


def token_for(
    user_id: UUID, *, expires_in: int = 3600, secret: str | None = None, audience: str = "authenticated"
) -> str:
    now = int(time.time())
    claims = {"sub": str(user_id), "aud": audience, "role": "authenticated", "iat": now, "exp": now + expires_in}
    return jwt.encode(claims, secret or get_settings().jwt_secret, algorithm="HS256")


def auth(user_id: UUID, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(user_id)}", **headers}


async def create_school(conn: AsyncConnection, name: str = "Test School") -> UUID:
    row = await fetch_one(
        conn,
        """INSERT INTO schools (name, school_code, market_region, timezone, country_code, status)
           VALUES (:n, :code, 'US', 'America/Phoenix', 'US', 'ACTIVE') RETURNING school_id""",
        n=name,
        code=f"S-{uuid.uuid4().hex[:8]}",
    )
    assert row
    return row["school_id"]


async def create_user(
    conn: AsyncConnection,
    display_name: str = "Test User",
    *,
    school_id: UUID | None = None,
    roles: tuple[str, ...] = (),
    status: str = "ACTIVE",
) -> UUID:
    row = await fetch_one(
        conn,
        """INSERT INTO users (email, display_name, account_status)
           VALUES (:email, :name, :status) RETURNING user_id""",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        name=display_name,
        status=status,
    )
    assert row
    user_id = row["user_id"]
    for code in roles:
        assert school_id is not None
        await execute(
            conn,
            """INSERT INTO school_user_roles (school_id, user_id, role_id)
               SELECT :s, :u, role_id FROM roles WHERE role_code = :c""",
            s=school_id,
            u=user_id,
            c=code,
        )
    return user_id


async def create_year_grade_section(
    conn: AsyncConnection, school_id: UUID, *, year_status: str = "ACTIVE"
) -> dict[str, UUID]:
    year = await fetch_one(
        conn,
        """INSERT INTO academic_years (school_id, name, start_date, end_date, status)
           VALUES (:s, :n, DATE '2026-06-01', DATE '2027-05-31', :st) RETURNING academic_year_id""",
        s=school_id,
        n=f"2026-27-{uuid.uuid4().hex[:4]}",
        st=year_status,
    )
    grade = await fetch_one(
        conn,
        """INSERT INTO grades (school_id, academic_year_id, name, grade_level, display_order)
           VALUES (:s, :y, 'Grade 10', 10, 10) RETURNING grade_id""",
        s=school_id,
        y=year["academic_year_id"],
    )
    section = await fetch_one(
        conn,
        """INSERT INTO sections (school_id, grade_id, name) VALUES (:s, :g, '10A')
           RETURNING section_id""",
        s=school_id,
        g=grade["grade_id"],
    )
    return {
        "academic_year_id": year["academic_year_id"],
        "grade_id": grade["grade_id"],
        "section_id": section["section_id"],
    }


async def create_student(
    conn: AsyncConnection, school_id: UUID, structure: dict[str, UUID], *, enrollment_status: str = "ACTIVE"
) -> tuple[UUID, UUID]:
    """Returns (user_id, student_id)."""
    user_id = await create_user(conn, "Student", school_id=school_id, roles=("STUDENT",))
    sp = await fetch_one(
        conn,
        """INSERT INTO student_profiles (school_id, user_id, admission_id, date_of_birth)
           VALUES (:s, :u, :a, DATE '2010-01-01') RETURNING student_id""",
        s=school_id,
        u=user_id,
        a=f"ADM-{uuid.uuid4().hex[:6]}",
    )
    await execute(
        conn,
        """INSERT INTO student_enrollments
             (school_id, student_id, academic_year_id, grade_id, section_id, enrollment_date, status)
           VALUES (:s, :st, :y, :g, :sec, DATE '2026-06-01', :es)""",
        s=school_id,
        st=sp["student_id"],
        y=structure["academic_year_id"],
        g=structure["grade_id"],
        sec=structure["section_id"],
        es=enrollment_status,
    )
    return user_id, sp["student_id"]
