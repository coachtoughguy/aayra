"""Contract §5 — minimal Admin bootstrap path.

Just enough surface to create a realistic school (structure, teachers, mappings, students)
through the API so Slice 1 can be exercised end to end. Full Admin screens are a later pass (§9).
"""

import hmac
import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app import audit, errors
from app.auth import Actor, current_actor
from app.config import get_settings
from app.context import RequestContext, get_context
from app.db import constraint_name, execute, fetch_one, get_db
from app.idempotency import OpClass, run_idempotent
from app.identity import LoginRequest, get_identity_provider
from app.outbox import emit

router = APIRouter(prefix="/v1", tags=["admin-bootstrap"])

MIN_LEARNER_AGE = 13  # doc 21, Sept 15 lock


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------
def _conflict(code: str, message: str, **details) -> errors.AppError:
    return errors.AppError(code, 409, message, details)


async def _school_of(conn: AsyncConnection, sql: str, entity: str, **params) -> dict:
    row = await fetch_one(conn, sql, **params)
    if row is None:
        raise errors.not_found(entity, **{k: str(v) for k, v in params.items()})
    return row


def _require_admin(actor: Actor, request: Request, school_id: UUID) -> None:
    request.state.school_id = str(school_id)
    actor.require_role(school_id, "SCHOOL_ADMIN")


async def _refresh_school_activation(conn: AsyncConnection, school_id: UUID) -> None:
    """doc 21 v1.0: a school activates once School Details + Academic Structure exist
    (an ACTIVE year with at least one grade, one section and one subject). Population is not
    a prerequisite."""
    await execute(
        conn,
        """UPDATE schools s SET status = 'ACTIVE'
            WHERE s.school_id = :s AND s.status = 'SETUP'
              AND EXISTS (
                SELECT 1 FROM academic_years ay
                  JOIN grades g ON g.academic_year_id = ay.academic_year_id
                  JOIN sections sec ON sec.grade_id = g.grade_id
                  JOIN subjects sub ON sub.academic_year_id = ay.academic_year_id
                 WHERE ay.school_id = s.school_id AND ay.status = 'ACTIVE')""",
        s=school_id,
    )


async def _create_user(
    conn: AsyncConnection,
    *,
    email: str | None,
    display_name: str,
    phone: str | None = None,
    date_of_birth: date | None = None,
    username: str | None = None,
) -> UUID:
    if email:
        existing = await fetch_one(conn, "SELECT user_id FROM users WHERE email = :e", e=email)
        if existing:  # one person, one account, many roles (doc 02)
            return existing["user_id"]
    user_id = await get_identity_provider().provision_login(
        LoginRequest(display_name=display_name, email=email, username=username)
    )
    await execute(
        conn,
        """INSERT INTO users (user_id, email, phone, display_name, date_of_birth, age_gate_status,
                              account_status)
           VALUES (:u, :e, :p, :n, :dob, :age, 'INVITED')""",
        u=user_id,
        e=email,
        p=phone,
        n=display_name,
        dob=date_of_birth,
        age="verified_13_plus" if date_of_birth else "not_applicable",
    )
    return user_id


async def _grant_role(conn: AsyncConnection, school_id: UUID, user_id: UUID, role: str, by: UUID | None) -> None:
    await execute(
        conn,
        """INSERT INTO school_user_roles (school_id, user_id, role_id, created_by)
           SELECT :s, :u, role_id, :by FROM roles WHERE role_code = :r
           ON CONFLICT (school_id, user_id, role_id) DO UPDATE SET status = 'ACTIVE'""",
        s=school_id,
        u=user_id,
        r=role,
        by=by,
    )


async def _record_invite(conn: AsyncConnection, user_id: UUID, invited_by: UUID) -> None:
    exists = await fetch_one(conn, "SELECT 1 AS x FROM account_activation_state WHERE user_id = :u", u=user_id)
    if exists:
        return
    now = datetime.now(UTC)
    await execute(
        conn,
        """INSERT INTO account_activation_state (user_id, state, invited_by, invite_sent_at, expires_at)
           VALUES (:u, 'invite_sent', :by, :now, :exp)""",
        u=user_id,
        by=invited_by,
        now=now,
        exp=now + timedelta(days=get_settings().invite_ttl_days),
    )


def _age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


# ------------------------------------------------------------------------------------------------
# POST /v1/schools — platform bootstrap (no admin exists yet)
# ------------------------------------------------------------------------------------------------
class AdminIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)


class SchoolIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    school_code: str = Field(min_length=2, max_length=50)
    market_region: str = Field(min_length=2, max_length=50)
    timezone: str = Field(min_length=3, max_length=50)
    country_code: str = Field(min_length=2, max_length=2)
    admin: AdminIn

    @field_validator("country_code")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


@router.post("/schools", status_code=201)
async def create_school(
    body: SchoolIn,
    request: Request,
    ctx: RequestContext = Depends(get_context),
    x_bootstrap_key: str | None = Header(default=None),
) -> JSONResponse:
    configured = get_settings().bootstrap_key
    if not configured or not x_bootstrap_key or not hmac.compare_digest(configured, x_bootstrap_key):
        raise errors.AppError("BOOTSTRAP_FORBIDDEN", 403, "School bootstrap requires the platform bootstrap key.")
    try:
        async with get_db().begin() as conn:
            school = await fetch_one(
                conn,
                """INSERT INTO schools (name, school_code, market_region, timezone, country_code)
                   VALUES (:n, :c, :m, :tz, :cc) RETURNING school_id, status""",
                n=body.name,
                c=body.school_code,
                m=body.market_region,
                tz=body.timezone,
                cc=body.country_code,
            )
            school_id = school["school_id"]
            admin_id = await _create_user(conn, email=body.admin.email, display_name=body.admin.display_name)
            await _grant_role(conn, school_id, admin_id, "SCHOOL_ADMIN", None)
            await _record_invite(conn, admin_id, invited_by=admin_id)  # platform-invited; self as inviter
            data = {
                "school_id": school_id,
                "status": school["status"],
                "admin_user_id": admin_id,
                "school_code": body.school_code,
            }
            await audit.record(
                conn,
                ctx,
                action="SCHOOL_BOOTSTRAPPED",
                entity_type="school",
                entity_id=school_id,
                school_id=school_id,
                actor_user_id=None,
                actor_role=None,
                after=data,
            )
            await emit(
                conn,
                ctx,
                event_type="SchoolCreated",
                aggregate_type="school",
                aggregate_id=school_id,
                school_id=school_id,
                data=data,
            )
    except IntegrityError as exc:
        if constraint_name(exc) == "schools_school_code_key":
            raise _conflict("SCHOOL_CODE_TAKEN", "A school with this code already exists.") from exc
        raise
    return JSONResponse(
        {"data": {k: str(v) for k, v in data.items()}, "meta": {"request_id": request.state.request_id}},
        status_code=201,
    )


# ------------------------------------------------------------------------------------------------
# Academic structure
# ------------------------------------------------------------------------------------------------
class AcademicYearIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    start_date: date
    end_date: date
    status: Literal["PLANNED", "ACTIVE"] = "ACTIVE"

    @model_validator(mode="after")
    def _dates(self):
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


@router.post("/schools/{school_id}/academic-years", status_code=201)
async def create_academic_year(
    school_id: UUID,
    body: AcademicYearIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        await _school_of(
            conn, "SELECT school_id FROM schools WHERE school_id = :school_id", "school", school_id=school_id
        )
    _require_admin(actor, request, school_id)

    async def handler(conn: AsyncConnection):
        if body.status == "ACTIVE":
            active = await fetch_one(
                conn,
                "SELECT academic_year_id FROM academic_years WHERE school_id = :s AND status = 'ACTIVE' FOR UPDATE",
                s=school_id,
            )
            if active:
                raise _conflict(
                    "ACADEMIC_YEAR_ALREADY_ACTIVE",
                    "This school already has an active academic year.",
                    academic_year_id=str(active["academic_year_id"]),
                )
        try:
            async with conn.begin_nested():
                row = await fetch_one(
                    conn,
                    """INSERT INTO academic_years (school_id, name, start_date, end_date, status)
                       VALUES (:s, :n, :sd, :ed, :st) RETURNING *""",
                    s=school_id,
                    n=body.name,
                    sd=body.start_date,
                    ed=body.end_date,
                    st=body.status,
                )
        except IntegrityError as exc:
            if constraint_name(exc) == "academic_years_school_id_name_key":
                raise _conflict("ACADEMIC_YEAR_NAME_TAKEN", "An academic year with this name exists.") from exc
            raise
        data = {
            "academic_year_id": row["academic_year_id"],
            "school_id": school_id,
            "name": row["name"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "status": row["status"],
        }
        await emit(
            conn,
            ctx,
            event_type="AcademicYearCreated",
            aggregate_type="academic_year",
            aggregate_id=row["academic_year_id"],
            school_id=school_id,
            data=data,
        )
        return 201, data

    return await run_idempotent(request, actor, body, handler, school_id=school_id)


class GradeIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    grade_level: int = Field(ge=1, le=20)
    display_order: int | None = None


@router.post("/academic-years/{academic_year_id}/grades", status_code=201)
async def create_grade(
    academic_year_id: UUID,
    body: GradeIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        year = await _school_of(
            conn,
            "SELECT school_id FROM academic_years WHERE academic_year_id = :academic_year_id",
            "academic_year",
            academic_year_id=academic_year_id,
        )
    school_id = year["school_id"]
    _require_admin(actor, request, school_id)

    async def handler(conn: AsyncConnection):
        row = await fetch_one(
            conn,
            """INSERT INTO grades (school_id, academic_year_id, name, grade_level, display_order)
               VALUES (:s, :y, :n, :lvl, :o) RETURNING grade_id, name, grade_level, display_order""",
            s=school_id,
            y=academic_year_id,
            n=body.name,
            lvl=body.grade_level,
            o=body.display_order if body.display_order is not None else body.grade_level,
        )
        data = {**row, "academic_year_id": academic_year_id, "school_id": school_id}
        await emit(
            conn,
            ctx,
            event_type="GradeCreated",
            aggregate_type="grade",
            aggregate_id=row["grade_id"],
            school_id=school_id,
            data=data,
        )
        return 201, data

    return await run_idempotent(request, actor, body, handler, school_id=school_id)


class SectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    capacity: int | None = Field(default=None, ge=1, le=500)


@router.post("/grades/{grade_id}/sections", status_code=201)
async def create_section(
    grade_id: UUID,
    body: SectionIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        grade = await _school_of(
            conn,
            "SELECT school_id, academic_year_id FROM grades WHERE grade_id = :grade_id",
            "grade",
            grade_id=grade_id,
        )
    school_id = grade["school_id"]
    _require_admin(actor, request, school_id)

    async def handler(conn: AsyncConnection):
        dup = await fetch_one(
            conn,
            "SELECT section_id FROM sections WHERE grade_id = :g AND lower(name) = lower(:n)",
            g=grade_id,
            n=body.name,
        )
        if dup:
            raise _conflict("SECTION_NAME_TAKEN", "This grade already has a section with that name.")
        row = await fetch_one(
            conn,
            """INSERT INTO sections (school_id, grade_id, name, capacity)
               VALUES (:s, :g, :n, :c) RETURNING section_id, name, capacity""",
            s=school_id,
            g=grade_id,
            n=body.name,
            c=body.capacity,
        )
        await _refresh_school_activation(conn, school_id)
        data = {**row, "grade_id": grade_id, "academic_year_id": grade["academic_year_id"], "school_id": school_id}
        await emit(
            conn,
            ctx,
            event_type="SectionCreated",
            aggregate_type="section",
            aggregate_id=row["section_id"],
            school_id=school_id,
            data=data,
        )
        return 201, data

    return await run_idempotent(request, actor, body, handler, school_id=school_id)


class SubjectIn(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    grade_ids: list[UUID] = Field(min_length=1)


@router.post("/academic-years/{academic_year_id}/subjects", status_code=201)
async def create_subject(
    academic_year_id: UUID,
    body: SubjectIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        year = await _school_of(
            conn,
            "SELECT school_id FROM academic_years WHERE academic_year_id = :academic_year_id",
            "academic_year",
            academic_year_id=academic_year_id,
        )
    school_id = year["school_id"]
    _require_admin(actor, request, school_id)

    async def handler(conn: AsyncConnection):
        grade_ids = list(dict.fromkeys(body.grade_ids))
        found = await fetch_one(
            conn,
            "SELECT count(*) AS n FROM grades WHERE grade_id = ANY(:ids) AND academic_year_id = :y",
            ids=grade_ids,
            y=academic_year_id,
        )
        if found["n"] != len(grade_ids):
            raise errors.validation_error("Every grade must belong to this academic year.", field="grade_ids")
        try:
            async with conn.begin_nested():
                row = await fetch_one(
                    conn,
                    """INSERT INTO subjects (school_id, academic_year_id, code, name, description)
                       VALUES (:s, :y, :c, :n, :d) RETURNING subject_id, code, name""",
                    s=school_id,
                    y=academic_year_id,
                    c=body.code,
                    n=body.name,
                    d=body.description,
                )
        except IntegrityError as exc:
            if constraint_name(exc) == "subjects_school_id_academic_year_id_code_key":
                raise _conflict("SUBJECT_CODE_TAKEN", "A subject with this code exists for this year.") from exc
            raise
        for gid in grade_ids:
            await execute(
                conn, "INSERT INTO grade_subjects (grade_id, subject_id) VALUES (:g, :s)", g=gid, s=row["subject_id"]
            )
        await _refresh_school_activation(conn, school_id)
        data = {**row, "academic_year_id": academic_year_id, "school_id": school_id, "grade_ids": grade_ids}
        await emit(
            conn,
            ctx,
            event_type="SubjectCreated",
            aggregate_type="subject",
            aggregate_id=row["subject_id"],
            school_id=school_id,
            data=data,
        )
        return 201, data

    return await run_idempotent(request, actor, body, handler, school_id=school_id)


# ------------------------------------------------------------------------------------------------
# People
# ------------------------------------------------------------------------------------------------
class TeacherIn(BaseModel):
    school_id: UUID
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    employee_number: str | None = Field(default=None, max_length=50)
    phone: str | None = Field(default=None, max_length=30)


@router.post("/teachers", status_code=201)
async def invite_teacher(
    body: TeacherIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        await _school_of(
            conn, "SELECT school_id FROM schools WHERE school_id = :school_id", "school", school_id=body.school_id
        )
    _require_admin(actor, request, body.school_id)

    async def handler(conn: AsyncConnection):
        user_id = await _create_user(conn, email=body.email, display_name=body.display_name, phone=body.phone)
        existing = await fetch_one(conn, "SELECT teacher_id FROM teacher_profiles WHERE user_id = :u", u=user_id)
        if existing:
            raise _conflict(
                "TEACHER_ALREADY_EXISTS",
                "This person already has a teacher profile.",
                teacher_id=str(existing["teacher_id"]),
            )
        try:
            async with conn.begin_nested():
                row = await fetch_one(
                    conn,
                    """INSERT INTO teacher_profiles (school_id, user_id, employee_number)
                       VALUES (:s, :u, :e) RETURNING teacher_id""",
                    s=body.school_id,
                    u=user_id,
                    e=body.employee_number,
                )
        except IntegrityError as exc:
            if constraint_name(exc) == "teacher_profiles_school_id_employee_number_key":
                raise _conflict("EMPLOYEE_NUMBER_TAKEN", "Another teacher has this employee number.") from exc
            raise
        await _grant_role(conn, body.school_id, user_id, "TEACHER", actor.user_id)
        await _record_invite(conn, user_id, invited_by=actor.user_id)
        data = {
            "teacher_id": row["teacher_id"],
            "user_id": user_id,
            "school_id": body.school_id,
            "display_name": body.display_name,
            "email": body.email,
        }
        await audit.record(
            conn,
            ctx,
            action="TEACHER_INVITED",
            entity_type="teacher_profile",
            entity_id=row["teacher_id"],
            school_id=body.school_id,
            actor_user_id=actor.user_id,
            actor_role="SCHOOL_ADMIN",
            after=data,
        )
        await emit(
            conn,
            ctx,
            event_type="TeacherInvited",
            aggregate_type="teacher",
            aggregate_id=row["teacher_id"],
            school_id=body.school_id,
            data=data,
        )
        return 201, data

    return await run_idempotent(request, actor, body, handler, school_id=body.school_id)


class TeacherAssignmentIn(BaseModel):
    teacher_id: UUID
    section_id: UUID
    subject_id: UUID | None = None
    assignment_role: Literal["CLASS_TEACHER", "PRIMARY_SUBJECT_TEACHER", "CO_TEACHER"]
    effective_from: date | None = None

    @model_validator(mode="after")
    def _subject_rule(self):
        if self.assignment_role == "CLASS_TEACHER" and self.subject_id is not None:
            raise ValueError("CLASS_TEACHER is section-wide and takes no subject_id")
        if self.assignment_role != "CLASS_TEACHER" and self.subject_id is None:
            raise ValueError(f"{self.assignment_role} requires subject_id")
        return self


@router.post("/teacher-assignments", status_code=201)
async def assign_teacher(
    body: TeacherAssignmentIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        section = await _school_of(
            conn,
            """SELECT sec.school_id, sec.grade_id, g.academic_year_id
                 FROM sections sec JOIN grades g ON g.grade_id = sec.grade_id
                WHERE sec.section_id = :section_id""",
            "section",
            section_id=body.section_id,
        )
    school_id = section["school_id"]
    _require_admin(actor, request, school_id)

    async def handler(conn: AsyncConnection):
        teacher = await fetch_one(
            conn,
            "SELECT teacher_id FROM teacher_profiles WHERE teacher_id = :t AND school_id = :s AND status = 'ACTIVE'",
            t=body.teacher_id,
            s=school_id,
        )
        if teacher is None:
            raise errors.not_found("teacher", teacher_id=str(body.teacher_id))
        if body.subject_id is not None:
            taught = await fetch_one(
                conn,
                "SELECT 1 AS x FROM grade_subjects WHERE grade_id = :g AND subject_id = :s",
                g=section["grade_id"],
                s=body.subject_id,
            )
            if taught is None:
                raise errors.validation_error("This subject is not offered in the section's grade.", field="subject_id")
        try:
            async with conn.begin_nested():
                row = await fetch_one(
                    conn,
                    """INSERT INTO teacher_section_subject_assignments
                         (school_id, academic_year_id, teacher_id, section_id, subject_id, assignment_role,
                          effective_from, created_by)
                       VALUES (:s, :y, :t, :sec, :sub, :role, :ef, :by)
                       RETURNING teacher_assignment_id, effective_from, status""",
                    s=school_id,
                    y=section["academic_year_id"],
                    t=body.teacher_id,
                    sec=body.section_id,
                    sub=body.subject_id,
                    role=body.assignment_role,
                    ef=body.effective_from or date.today(),
                    by=actor.user_id,
                )
        except IntegrityError as exc:
            name = constraint_name(exc)
            if name == "idx_tssa_one_active_class_teacher":
                raise _conflict(
                    "CLASS_TEACHER_ALREADY_ASSIGNED", "This section already has an active Class Teacher."
                ) from exc
            if name == "idx_tssa_one_active_primary_subject_teacher":
                raise _conflict(
                    "PRIMARY_TEACHER_ALREADY_ASSIGNED",
                    "This section and subject already have an active Primary Subject Teacher.",
                ) from exc
            raise
        data = {
            "teacher_assignment_id": row["teacher_assignment_id"],
            "teacher_id": body.teacher_id,
            "section_id": body.section_id,
            "subject_id": body.subject_id,
            "assignment_role": body.assignment_role,
            "academic_year_id": section["academic_year_id"],
            "school_id": school_id,
            "effective_from": row["effective_from"],
            "status": row["status"],
        }
        await audit.record(
            conn,
            ctx,
            action="TEACHER_ASSIGNED",
            entity_type="teacher_section_subject_assignment",
            entity_id=row["teacher_assignment_id"],
            school_id=school_id,
            actor_user_id=actor.user_id,
            actor_role="SCHOOL_ADMIN",
            after=data,
        )
        await emit(
            conn,
            ctx,
            event_type="TeacherAssigned",
            aggregate_type="teacher_assignment",
            aggregate_id=row["teacher_assignment_id"],
            school_id=school_id,
            data=data,
        )
        return 201, data

    return await run_idempotent(request, actor, body, handler, school_id=school_id)


_USERNAME_SAFE = re.compile(r"[^a-z0-9]+")


class StudentIn(BaseModel):
    section_id: UUID
    display_name: str = Field(min_length=1, max_length=200)
    admission_id: str = Field(min_length=1, max_length=50)
    date_of_birth: date
    email: EmailStr | None = None
    preferred_language: str | None = Field(default=None, max_length=20)
    enrollment_date: date | None = None


@router.post("/students", status_code=201)
async def create_student(
    body: StudentIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        section = await _school_of(
            conn,
            """SELECT sec.school_id, sec.grade_id, g.academic_year_id, s.school_code
                 FROM sections sec JOIN grades g ON g.grade_id = sec.grade_id
                 JOIN schools s ON s.school_id = sec.school_id
                WHERE sec.section_id = :section_id""",
            "section",
            section_id=body.section_id,
        )
    school_id = section["school_id"]
    _require_admin(actor, request, school_id)

    enrollment_date = body.enrollment_date or date.today()
    if _age_on(body.date_of_birth, enrollment_date) < MIN_LEARNER_AGE:
        raise errors.AppError(
            "AGE_BELOW_MINIMUM",
            422,
            f"Learners must be at least {MIN_LEARNER_AGE} years old.",
            {"minimum_age": MIN_LEARNER_AGE},
        )

    async def handler(conn: AsyncConnection):
        dup = await fetch_one(
            conn,
            "SELECT student_id FROM student_profiles WHERE school_id = :s AND admission_id = :a",
            s=school_id,
            a=body.admission_id,
        )
        if dup:
            raise _conflict(
                "ADMISSION_ID_TAKEN",
                "A student with this admission id already exists.",
                student_id=str(dup["student_id"]),
            )
        username = (
            None
            if body.email
            else f"{_USERNAME_SAFE.sub('', section['school_code'].lower())}."
            f"{_USERNAME_SAFE.sub('', body.admission_id.lower())}"
        )
        user_id = await _create_user(
            conn,
            email=body.email,
            display_name=body.display_name,
            date_of_birth=body.date_of_birth,
            username=username,
        )
        profile = await fetch_one(
            conn,
            """INSERT INTO student_profiles (school_id, user_id, admission_id, date_of_birth, preferred_language)
               VALUES (:s, :u, :a, :dob, :lang) RETURNING student_id""",
            s=school_id,
            u=user_id,
            a=body.admission_id,
            dob=body.date_of_birth,
            lang=body.preferred_language,
        )
        student_id = profile["student_id"]
        await _grant_role(conn, school_id, user_id, "STUDENT", actor.user_id)
        await _record_invite(conn, user_id, invited_by=actor.user_id)
        enrollment = await fetch_one(
            conn,
            """INSERT INTO student_enrollments
                 (school_id, student_id, academic_year_id, grade_id, section_id, enrollment_date, status)
               VALUES (:s, :st, :y, :g, :sec, :d, 'ACTIVE') RETURNING enrollment_id""",
            s=school_id,
            st=student_id,
            y=section["academic_year_id"],
            g=section["grade_id"],
            sec=body.section_id,
            d=enrollment_date,
        )
        data = {
            "student_id": student_id,
            "user_id": user_id,
            "enrollment_id": enrollment["enrollment_id"],
            "school_id": school_id,
            "section_id": body.section_id,
            "academic_year_id": section["academic_year_id"],
            "display_name": body.display_name,
            "admission_id": body.admission_id,
            "username": username,
            "guardian_status": "PRIMARY_GUARDIAN_PENDING",  # never blocks learning (doc 21 v1.0)
        }
        await audit.record(
            conn,
            ctx,
            action="STUDENT_ENROLLED",
            entity_type="student_profile",
            entity_id=student_id,
            school_id=school_id,
            actor_user_id=actor.user_id,
            actor_role="SCHOOL_ADMIN",
            after={k: v for k, v in data.items() if k != "username"},
        )
        enrolled_event = await emit(
            conn,
            ctx,
            event_type="StudentCreated",
            aggregate_type="student",
            aggregate_id=student_id,
            school_id=school_id,
            data=data,
        )
        # Contract §5/§9/§11: every path that yields an ACTIVE enrollment emits this, so the
        # late-enrollment back-fill (§11) fires uniformly.
        await emit(
            conn,
            ctx,
            event_type="StudentEnrollmentActivated",
            aggregate_type="student_enrollment",
            aggregate_id=enrollment["enrollment_id"],
            school_id=school_id,
            causation_id=enrolled_event,
            data={
                "student_id": student_id,
                "section_id": body.section_id,
                "academic_year_id": section["academic_year_id"],
                "effective_at": enrollment_date,
            },
        )
        return 201, data

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.DEFAULT, school_id=school_id)
