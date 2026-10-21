"""Independent adult self-learners (docs 02 §3.4 and 05 §18.5, Sept 15 locks; Oct 20 decision).

Direct signup without a school is for adults (18+) only. A learner under 18 is routed to the school
path (guardian linkage and school consent apply) instead.

Each independent learner gets a private personal space, stored as a `schools` row with
`school_kind = 'INDEPENDENT'` and the learner as `owner_user_id`. It holds one open-ended
"Personal" year, grade and section, the learner's own subjects, and an ACTIVE enrollment. So student
tenancy, privacy and every self-learning endpoint work exactly as for school students: there are
no teachers, guardians or school assignments in it. Mastery in it is SELF-scoped.

The exact age-verification mechanism is still OPEN (doc 02); until then the learner's own
date-of-birth declaration is recorded (`users.age_gate_status = 'self_declared_18_plus'`) and audited.
"""

import re
from datetime import UTC, date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app import audit, errors
from app.auth import Actor, Identity, current_actor, current_identity, resolve_student_context
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one, get_db
from app.idempotency import run_idempotent
from app.outbox import emit

router = APIRouter(prefix="/v1", tags=["independent-learners"])

MIN_INDEPENDENT_AGE = 18
SPACE_YEARS = 10  # the personal year is open-ended in practice; no rollover for independent learners
MAX_SUBJECTS = 30


def age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def subject_code(name: str) -> str:
    return (re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-") or "SUBJECT")[:50]


class SignupIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    date_of_birth: date
    confirms_18_or_older: bool
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    timezone: str = Field(min_length=1, max_length=50)
    preferred_language: str | None = Field(default=None, max_length=20)
    subjects: list[str] = Field(default_factory=list, max_length=MAX_SUBJECTS)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone") from exc
        return v

    @field_validator("subjects")
    @classmethod
    def _subjects(cls, v: list[str]) -> list[str]:
        names = [n.strip() for n in v if n.strip()]
        if any(len(n) > 150 for n in names):
            raise ValueError("subject names are at most 150 characters")
        return list(dict.fromkeys(names))


async def _add_subject(conn: AsyncConnection, space: dict, name: str, description: str | None = None) -> dict:
    code = subject_code(name)
    exists = await fetch_one(
        conn, "SELECT subject_id FROM subjects WHERE academic_year_id = :y AND code = :c",
        y=space["academic_year_id"], c=code,
    )  # fmt: skip
    if exists:
        raise errors.AppError(
            "SUBJECT_EXISTS", 409, "You already have this subject.", {"subject_id": str(exists["subject_id"])}
        )
    row = await fetch_one(
        conn,
        """INSERT INTO subjects (school_id, academic_year_id, code, name, description)
           VALUES (:s, :y, :c, :n, :d) RETURNING subject_id, code, name""",
        s=space["school_id"], y=space["academic_year_id"], c=code, n=name, d=description,
    )  # fmt: skip
    await execute(
        conn, "INSERT INTO grade_subjects (grade_id, subject_id, is_required) VALUES (:g, :s, false)",
        g=space["grade_id"], s=row["subject_id"],
    )  # fmt: skip
    return row


@router.post("/independent-learners", status_code=201)
async def sign_up(
    body: SignupIn,
    request: Request,
    identity: Identity = Depends(current_identity),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    today = datetime.now(UTC).date()
    age = age_on(body.date_of_birth, today)
    if age < MIN_INDEPENDENT_AGE:
        raise errors.AppError(
            "AGE_RESTRICTED", 403,
            "Learners under 18 use Aayra through their school, so a parent or guardian can be linked.",
            {"minimum_age": MIN_INDEPENDENT_AGE, "route": "SCHOOL_AFFILIATION"},
        )  # fmt: skip
    if not body.confirms_18_or_older:
        raise errors.validation_error("Please confirm you are 18 or older.", field="confirms_18_or_older")
    actor = Actor(user_id=identity.user_id, display_name=body.display_name)

    async def handler(conn: AsyncConnection):
        user = await fetch_one(conn, "SELECT * FROM users WHERE user_id = :u FOR UPDATE", u=identity.user_id)
        if user is not None:
            if user["account_status"] not in ("ACTIVE", "INVITED"):
                raise errors.AppError("ACCOUNT_INACTIVE", 403, "This account is not active.")
            student = await fetch_one(
                conn,
                """SELECT s.school_kind FROM student_profiles sp JOIN schools s USING (school_id)
                    WHERE sp.user_id = :u""",
                u=identity.user_id,
            )
            if student is not None:
                raise errors.AppError(
                    "ALREADY_A_STUDENT", 409,
                    "You already have a student account" + (
                        "." if student["school_kind"] == "INDEPENDENT" else "; use self-learning inside your school."),
                    {"school_kind": student["school_kind"]},
                )  # fmt: skip
            # One person, one account (doc 02 §3.1): a teacher or parent can also be an adult self-learner.
            await execute(
                conn,
                """UPDATE users SET date_of_birth = coalesce(date_of_birth, :dob), age_gate_status = 'self_declared_18_plus',
                          account_status = 'ACTIVE', last_login_at = now() WHERE user_id = :u""",
                dob=body.date_of_birth, u=identity.user_id,
            )  # fmt: skip
        else:
            if identity.email:
                taken = await fetch_one(conn, "SELECT 1 AS x FROM users WHERE email = :e", e=identity.email)
                if taken:
                    raise errors.AppError("EMAIL_IN_USE", 409, "This email already belongs to another Aayra account.")
            await execute(
                conn,
                """INSERT INTO users (user_id, email, display_name, date_of_birth, age_gate_status, account_status,
                                      last_login_at)
                   VALUES (:u, :e, :n, :dob, 'self_declared_18_plus', 'ACTIVE', now())""",
                u=identity.user_id, e=identity.email, n=body.display_name, dob=body.date_of_birth,
            )  # fmt: skip
        market = await fetch_one(
            conn,
            """SELECT market_region FROM market_compliance_profiles WHERE market_region IN (:c, 'GLOBAL')
                ORDER BY (market_region = :c) DESC LIMIT 1""",
            c=body.country_code,
        )
        school = await fetch_one(
            conn,
            """INSERT INTO schools (name, school_code, market_region, timezone, country_code, status, school_kind,
                                    owner_user_id)
               VALUES (:n, :code, :m, :tz, :cc, 'ACTIVE', 'INDEPENDENT', :u) RETURNING school_id""",
            n=f"{body.display_name} - personal learning"[:200], code=f"IND-{uuid4().hex[:12].upper()}",
            m=market["market_region"] if market else "GLOBAL", tz=body.timezone, cc=body.country_code,
            u=identity.user_id,
        )  # fmt: skip
        school_id = school["school_id"]
        year = await fetch_one(
            conn,
            """INSERT INTO academic_years (school_id, name, start_date, end_date, status)
               VALUES (:s, 'Personal', :start, :end, 'ACTIVE') RETURNING academic_year_id""",
            s=school_id, start=today, end=date(today.year + SPACE_YEARS, today.month, min(today.day, 28)),
        )  # fmt: skip
        grade = await fetch_one(
            conn,
            """INSERT INTO grades (school_id, academic_year_id, name, grade_level, display_order)
               VALUES (:s, :y, 'Personal learning', 0, 1) RETURNING grade_id""",
            s=school_id, y=year["academic_year_id"],
        )  # fmt: skip
        section = await fetch_one(
            conn, "INSERT INTO sections (school_id, grade_id, name) VALUES (:s, :g, 'Personal') RETURNING section_id",
            s=school_id, g=grade["grade_id"],
        )  # fmt: skip
        profile = await fetch_one(
            conn,
            """INSERT INTO student_profiles (school_id, user_id, admission_id, date_of_birth, preferred_language)
               VALUES (:s, :u, 'SELF', :dob, :lang) RETURNING student_id""",
            s=school_id, u=identity.user_id, dob=body.date_of_birth, lang=body.preferred_language,
        )  # fmt: skip
        await execute(
            conn,
            """INSERT INTO school_user_roles (school_id, user_id, role_id, created_by)
               SELECT :s, :u, role_id, :u FROM roles WHERE role_code = 'STUDENT'""",
            s=school_id, u=identity.user_id,
        )  # fmt: skip
        await execute(
            conn,
            """INSERT INTO student_enrollments (school_id, student_id, academic_year_id, grade_id, section_id,
                                                enrollment_date, status)
               VALUES (:s, :st, :y, :g, :sec, :d, 'ACTIVE')""",
            s=school_id, st=profile["student_id"], y=year["academic_year_id"], g=grade["grade_id"],
            sec=section["section_id"], d=today,
        )  # fmt: skip
        space = {"school_id": school_id, "academic_year_id": year["academic_year_id"], "grade_id": grade["grade_id"]}
        subjects = [await _add_subject(conn, space, n) for n in body.subjects]
        data = {"school_id": school_id, "student_id": profile["student_id"], "user_id": identity.user_id,
                "academic_year_id": year["academic_year_id"], "subjects": subjects}  # fmt: skip
        await audit.record(
            conn, ctx, action="INDEPENDENT_LEARNER_SIGNED_UP", entity_type="student_profile",
            entity_id=profile["student_id"], school_id=school_id, actor_user_id=identity.user_id, actor_role="STUDENT",
            after={"age_gate_status": "self_declared_18_plus", "declared_age": age, "country_code": body.country_code},
        )  # fmt: skip
        await emit(
            conn, ctx, event_type="IndependentLearnerSignedUp", aggregate_type="student",
            aggregate_id=profile["student_id"], school_id=school_id,
            data={"student_id": profile["student_id"], "country_code": body.country_code, "subjects": len(subjects)},
        )  # fmt: skip
        return 201, data

    return await run_idempotent(request, actor, body, handler)


async def require_independent(conn: AsyncConnection, actor: Actor) -> dict:
    sctx = await resolve_student_context(conn, actor)
    space = await fetch_one(
        conn, "SELECT school_id, school_kind, owner_user_id, name, timezone FROM schools WHERE school_id = :s",
        s=sctx.school_id,
    )  # fmt: skip
    if space["school_kind"] != "INDEPENDENT" or space["owner_user_id"] != actor.user_id:
        raise errors.AppError(
            "NOT_INDEPENDENT", 409, "This is for independent learners; your subjects come from your school."
        )
    return {
        **space,
        "student_id": sctx.student_id,
        "academic_year_id": sctx.academic_year_id,
        "grade_id": sctx.grade_id,
    }


async def _subjects(conn: AsyncConnection, grade_id: UUID) -> list[dict]:
    return await fetch_all(
        conn,
        """SELECT s.subject_id, s.code, s.name FROM grade_subjects gs JOIN subjects s USING (subject_id)
            WHERE gs.grade_id = :g ORDER BY s.name""",
        g=grade_id,
    )


@router.get("/independent-learners/me")
async def my_space(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        space = await require_independent(conn, actor)
        subjects = await _subjects(conn, space["grade_id"])
    return {"data": {"school_id": space["school_id"], "student_id": space["student_id"], "name": space["name"],
                     "timezone": space["timezone"], "subjects": subjects},
            "meta": {"request_id": request.state.request_id}}  # fmt: skip


class SubjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2000)


@router.post("/independent-learners/me/subjects", status_code=201)
async def add_subject(
    body: SubjectIn, request: Request, actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context)
) -> JSONResponse:
    async with get_db().connect() as conn:
        space = await require_independent(conn, actor)
    request.state.school_id = str(space["school_id"])
    if len(await _count_subjects(space)) >= MAX_SUBJECTS:
        raise errors.AppError("TOO_MANY_SUBJECTS", 409, f"Up to {MAX_SUBJECTS} subjects.")

    async def handler(conn: AsyncConnection):
        return 201, await _add_subject(conn, space, body.name.strip(), body.description)

    return await run_idempotent(request, actor, body, handler, school_id=space["school_id"])


async def _count_subjects(space: dict) -> list[dict]:
    async with get_db().connect() as conn:
        return await _subjects(conn, space["grade_id"])


@router.get("/students/me/subjects")
async def my_subjects(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    """Any student's subjects (for choosing one when creating self-learning)."""
    async with get_db().connect() as conn:
        sctx = await resolve_student_context(conn, actor)
        subjects = await _subjects(conn, sctx.grade_id)
    return {"data": {"subjects": subjects}, "meta": {"request_id": request.state.request_id}}
