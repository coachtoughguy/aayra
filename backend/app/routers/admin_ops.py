"""School Admin operations (docs 02/14/21 MVP): bulk student import with a per-row validation summary,
section transfer, withdrawal, academic-year rollover, the operational Admin home ("Needs Attention"),
and the lists the Admin screens need.

Admin authority is operational: these endpoints never edit teaching content or grades (doc 21).
"""

import csv
import io
import json
import re
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from app import audit, errors
from app.auth import Actor, current_actor
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one, get_db
from app.idempotency import run_idempotent
from app.outbox import emit
from app.routers.admin_bootstrap import MIN_LEARNER_AGE, StudentIn, _age_on, _require_admin, enroll_student
from app.services.distribution import backfill_student

router = APIRouter(prefix="/v1", tags=["admin-ops"])

MAX_IMPORT_ROWS = 500
COLUMNS = ("admission_id", "display_name", "date_of_birth", "section", "email", "preferred_language")


async def _school(conn: AsyncConnection, school_id: UUID) -> dict:
    row = await fetch_one(conn, "SELECT * FROM schools WHERE school_id = :s", s=school_id)
    if row is None:
        raise errors.not_found("school", school_id=str(school_id))
    return row


async def _active_year(conn: AsyncConnection, school_id: UUID) -> dict | None:
    return await fetch_one(
        conn,
        "SELECT * FROM academic_years WHERE school_id = :s AND status = 'ACTIVE' ORDER BY start_date DESC LIMIT 1",
        s=school_id,
    )


# ------------------------------------------------------------------------------------------------
# Lists and Admin home
# ------------------------------------------------------------------------------------------------
@router.get("/schools/{school_id}/structure")
async def structure(school_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    _require_admin(actor, request, school_id)
    async with get_db().connect() as conn:
        school = await _school(conn, school_id)
        years = await fetch_all(
            conn, "SELECT * FROM academic_years WHERE school_id = :s ORDER BY start_date DESC", s=school_id
        )
        for y in years:
            y["grades"] = await fetch_all(
                conn,
                """SELECT grade_id, name, grade_level, display_order FROM grades WHERE academic_year_id = :y
                    ORDER BY display_order, grade_level""",
                y=y["academic_year_id"],
            )
            for g in y["grades"]:
                g["sections"] = await fetch_all(
                    conn,
                    """SELECT sec.section_id, sec.name,
                              (SELECT count(*) FROM student_enrollments se
                                WHERE se.section_id = sec.section_id AND se.status = 'ACTIVE') AS active_students,
                              (SELECT u.display_name FROM teacher_section_subject_assignments t
                                 JOIN teacher_profiles tp USING (teacher_id) JOIN users u ON u.user_id = tp.user_id
                                WHERE t.section_id = sec.section_id AND t.assignment_role = 'CLASS_TEACHER'
                                  AND t.status = 'ACTIVE' LIMIT 1) AS class_teacher
                         FROM sections sec WHERE sec.grade_id = :g ORDER BY sec.name""",
                    g=g["grade_id"],
                )
            y["subjects"] = await fetch_all(
                conn, "SELECT subject_id, code, name FROM subjects WHERE academic_year_id = :y ORDER BY name",
                y=y["academic_year_id"],
            )  # fmt: skip
    return {"data": {"school": school, "academic_years": years}, "meta": {"request_id": request.state.request_id}}


@router.get("/sections/{section_id}/students")
async def section_students(
    section_id: UUID, request: Request, actor: Actor = Depends(current_actor), include_inactive: bool = False
) -> dict:
    async with get_db().connect() as conn:
        sec = await fetch_one(conn, "SELECT school_id, name FROM sections WHERE section_id = :s", s=section_id)
        if sec is None:
            raise errors.not_found("section", section_id=str(section_id))
        _require_admin(actor, request, sec["school_id"])
        rows = await fetch_all(
            conn,
            f"""SELECT sp.student_id, u.display_name, sp.admission_id, u.email, u.account_status, se.status AS enrollment_status,
                       se.enrollment_date, se.exit_date,
                       (SELECT count(*) FROM student_guardians g WHERE g.student_id = sp.student_id AND g.status = 'ACTIVE')
                         AS guardians
                  FROM student_enrollments se JOIN student_profiles sp USING (student_id) JOIN users u ON u.user_id = sp.user_id
                 WHERE se.section_id = :s {"" if include_inactive else "AND se.status = 'ACTIVE'"}
                 ORDER BY u.display_name""",
            s=section_id,
        )
    for r in rows:
        r["guardian_status"] = "LINKED" if r.pop("guardians") else "PRIMARY_GUARDIAN_PENDING"
    return {"data": {"section": sec, "students": rows}, "meta": {"request_id": request.state.request_id}}


@router.get("/schools/{school_id}/teachers")
async def school_teachers(school_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    _require_admin(actor, request, school_id)
    async with get_db().connect() as conn:
        teachers = await fetch_all(
            conn,
            """SELECT tp.teacher_id, tp.user_id, u.display_name, u.email, u.account_status FROM teacher_profiles tp
                 JOIN users u ON u.user_id = tp.user_id WHERE tp.school_id = :s ORDER BY u.display_name""",
            s=school_id,
        )
        for t in teachers:
            t["assignments"] = await fetch_all(
                conn,
                """SELECT t.teacher_assignment_id, t.assignment_role, sec.name AS section_name, sub.name AS subject_name,
                          t.section_id, t.subject_id
                     FROM teacher_section_subject_assignments t JOIN sections sec USING (section_id)
                     LEFT JOIN subjects sub ON sub.subject_id = t.subject_id
                    WHERE t.teacher_id = :t AND t.status = 'ACTIVE' ORDER BY sec.name, sub.name""",
                t=t["teacher_id"],
            )
    return {"data": {"teachers": teachers}, "meta": {"request_id": request.state.request_id}}


@router.get("/schools/{school_id}/admin/home")
async def admin_home(school_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    """Operational Admin home (doc 21 LOCKED: Setup, People, Mappings, Needs Attention, Quick Actions).
    No academic/performance intelligence here."""
    _require_admin(actor, request, school_id)
    async with get_db().connect() as conn:
        school = await _school(conn, school_id)
        year = await _active_year(conn, school_id)
        y = year["academic_year_id"] if year else None
        counts = await fetch_one(
            conn,
            """SELECT
                 (SELECT count(*) FROM student_enrollments WHERE school_id = :s AND academic_year_id = :y
                     AND status = 'ACTIVE') AS active_students,
                 (SELECT count(*) FROM teacher_profiles WHERE school_id = :s) AS teachers,
                 (SELECT count(*) FROM student_enrollments se WHERE se.school_id = :s AND se.academic_year_id = :y
                     AND se.status = 'ACTIVE' AND NOT EXISTS (SELECT 1 FROM student_guardians g
                        WHERE g.student_id = se.student_id AND g.status = 'ACTIVE')) AS students_without_guardian,
                 (SELECT count(*) FROM sections sec JOIN grades g USING (grade_id) WHERE g.academic_year_id = :y
                     AND NOT EXISTS (SELECT 1 FROM teacher_section_subject_assignments t WHERE t.section_id = sec.section_id
                        AND t.assignment_role = 'CLASS_TEACHER' AND t.status = 'ACTIVE')) AS sections_without_class_teacher,
                 (SELECT count(*) FROM sections sec JOIN grades g USING (grade_id)
                     JOIN grade_subjects gs ON gs.grade_id = g.grade_id
                    WHERE g.academic_year_id = :y AND NOT EXISTS (SELECT 1 FROM teacher_section_subject_assignments t
                        WHERE t.section_id = sec.section_id AND t.subject_id = gs.subject_id
                          AND t.assignment_role = 'PRIMARY_SUBJECT_TEACHER' AND t.status = 'ACTIVE'))
                   AS section_subjects_without_primary_teacher,
                 (SELECT count(*) FROM account_activation_state a JOIN school_user_roles r ON r.user_id = a.user_id
                     AND r.school_id = :s AND r.status = 'ACTIVE'
                    WHERE a.state <> 'activated') AS invites_pending,
                 (SELECT count(*) FROM csv_import_batches WHERE school_id = :s AND status IN ('VALIDATING',
                     'VALIDATED_WITH_ERRORS')) AS imports_waiting""",
            s=school_id,
            y=y,
        )
    needs = [
        {"key": k, "count": counts[k]}
        for k in (
            "students_without_guardian",
            "sections_without_class_teacher",
            "section_subjects_without_primary_teacher",
            "invites_pending",
            "imports_waiting",
        )  # fmt: skip
        if counts[k]
    ]
    return {
        "data": {"school": {"school_id": school["school_id"], "name": school["name"], "status": school["status"]},
                 "active_year": year and {"academic_year_id": y, "name": year["name"]},
                 "people": {"active_students": counts["active_students"], "teachers": counts["teachers"]},
                 "needs_attention": needs},
        "meta": {"request_id": request.state.request_id},
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------
# Bulk student import (doc 02 Sept 9: per-row validation; valid rows commit even if others fail)
# ------------------------------------------------------------------------------------------------
class ImportIn(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    csv_text: str = Field(min_length=1, max_length=2_000_000)


_EMAIL = TypeAdapter(EmailStr)


def _parse_rows(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None:
        raise errors.validation_error("The file is empty.")
    headers = [h.strip().lower().replace(" ", "_") for h in reader.fieldnames]
    missing = [c for c in ("admission_id", "display_name", "date_of_birth", "section") if c not in headers]
    if missing:
        raise errors.validation_error("Missing columns: " + ", ".join(missing), expected_columns=list(COLUMNS))
    rows = []
    for raw in reader:
        rows.append({h: (v or "").strip() for h, v in zip(headers, raw.values(), strict=False) if h in COLUMNS})
    if len(rows) > MAX_IMPORT_ROWS:
        raise errors.validation_error(f"Up to {MAX_IMPORT_ROWS} students per file.")
    return rows


async def _validate(
    conn: AsyncConnection, school_id: UUID, year_id: UUID, rows: list[dict]
) -> list[tuple[str | None, str | None]]:
    sections = {r["name"].lower(): r for r in await fetch_all(
        conn, """SELECT sec.section_id, sec.name FROM sections sec JOIN grades g USING (grade_id)
                  WHERE g.academic_year_id = :y""", y=year_id)}  # fmt: skip
    existing = {r["admission_id"].lower() for r in await fetch_all(
        conn, "SELECT admission_id FROM student_profiles WHERE school_id = :s", s=school_id)}  # fmt: skip
    seen: set[str] = set()
    out = []
    today = date.today()
    for r in rows:
        err = None
        if not r.get("admission_id") or not r.get("display_name"):
            err = ("missing_required_field", "Admission id and name are required.")
        elif r["admission_id"].lower() in existing:
            err = ("duplicate_admission_id", "A student with this admission id already exists.")
        elif r["admission_id"].lower() in seen:
            err = ("duplicate_in_file", "This admission id appears twice in the file.")
        elif r.get("section", "").lower() not in sections:
            err = ("invalid_section", f"No section named '{r.get('section')}' in the active year.")
        else:
            try:
                dob = date.fromisoformat(r["date_of_birth"])
            except ValueError:
                err = ("invalid_date_of_birth", "Use YYYY-MM-DD for the date of birth.")
            else:
                if _age_on(dob, today) < MIN_LEARNER_AGE:
                    err = ("below_minimum_age", f"Learners must be at least {MIN_LEARNER_AGE}.")
            if err is None and r.get("email"):
                try:
                    _EMAIL.validate_python(r["email"])
                except ValidationError:
                    err = ("invalid_email", "The email address isn't valid.")
        seen.add((r.get("admission_id") or "").lower())
        out.append(err or (None, None))
        if err is None:
            r["section_id"] = str(sections[r["section"].lower()]["section_id"])
    return out


@router.post("/schools/{school_id}/student-imports", status_code=201)
async def validate_import(
    school_id: UUID, body: ImportIn, request: Request, actor: Actor = Depends(current_actor)
) -> JSONResponse:
    _require_admin(actor, request, school_id)
    rows = _parse_rows(body.csv_text)

    async def handler(conn: AsyncConnection):
        year = await _active_year(conn, school_id)
        if year is None:
            raise errors.AppError("NO_ACTIVE_YEAR", 409, "Set up and activate an academic year first.")
        results = await _validate(conn, school_id, year["academic_year_id"], rows)
        errors_n = sum(1 for code, _ in results if code)
        batch = await fetch_one(
            conn,
            """INSERT INTO csv_import_batches (school_id, imported_by, file_name, import_type, status, submitted_at)
               VALUES (:s, :u, :f, 'STUDENTS', :st, now()) RETURNING batch_id""",
            # 'VALIDATED_WITH_ERRORS' (21 chars) doesn't fit the column's varchar(20); a validated batch
            # awaiting commit stays VALIDATING and per-row results carry the errors.
            s=school_id, u=actor.user_id, f=body.file_name, st="VALIDATING",
        )  # fmt: skip
        report = []
        for n, (r, (code, msg)) in enumerate(zip(rows, results, strict=True), start=2):  # row 1 is the header
            await execute(
                conn,
                """INSERT INTO csv_import_row_results (batch_id, row_number, raw_row_data, validation_status, error_reason)
                   VALUES (:b, :n, CAST(:raw AS jsonb), :vs, :e)""",
                b=batch["batch_id"], n=n, raw=json.dumps(r), vs="ERROR" if code else "VALID", e=code,
            )  # fmt: skip
            if code:
                report.append(
                    {"row_number": n, "admission_id": r.get("admission_id"), "error_reason": code, "message": msg}
                )
        return 201, {"batch_id": batch["batch_id"], "total_rows": len(rows), "valid_rows": len(rows) - errors_n,
                     "error_rows": errors_n, "errors": report}  # fmt: skip

    return await run_idempotent(request, actor, body, handler, school_id=school_id)


@router.post("/student-imports/{batch_id}/commit")
async def commit_import(
    batch_id: UUID, request: Request, actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context)
) -> JSONResponse:
    async with get_db().connect() as conn:
        batch = await fetch_one(conn, "SELECT * FROM csv_import_batches WHERE batch_id = :b", b=batch_id)
    if batch is None:
        raise errors.not_found("import", batch_id=str(batch_id))
    _require_admin(actor, request, batch["school_id"])

    async def handler(conn: AsyncConnection):
        live = await fetch_one(conn, "SELECT * FROM csv_import_batches WHERE batch_id = :b FOR UPDATE", b=batch_id)
        if live["status"] not in ("VALIDATING", "VALIDATED_WITH_ERRORS"):
            raise errors.AppError("IMPORT_ALREADY_COMMITTED", 409, "This import was already committed.")
        school = await _school(conn, live["school_id"])
        rows = await fetch_all(
            conn, "SELECT * FROM csv_import_row_results WHERE batch_id = :b AND validation_status = 'VALID' ORDER BY row_number",
            b=batch_id,
        )  # fmt: skip
        created, logins = 0, []
        for row in rows:
            r = row["raw_row_data"]
            sec = await fetch_one(
                conn, "SELECT sec.school_id, sec.grade_id, g.academic_year_id FROM sections sec JOIN grades g USING (grade_id) "
                      "WHERE sec.section_id = :s", s=UUID(r["section_id"]),
            )  # fmt: skip
            body = StudentIn(section_id=r["section_id"], display_name=r["display_name"], admission_id=r["admission_id"],
                             date_of_birth=r["date_of_birth"], email=r.get("email") or None,
                             preferred_language=r.get("preferred_language") or None)  # fmt: skip
            try:
                async with (
                    conn.begin_nested()
                ):  # one bad row (e.g. a duplicate created meanwhile) doesn't sink the rest
                    data = await enroll_student(conn, ctx, actor, {**sec, "school_code": school["school_code"]}, body)
            except errors.AppError as exc:
                await execute(conn, "UPDATE csv_import_row_results SET validation_status = 'ERROR', error_reason = :e "
                                    "WHERE row_result_id = :r", e=exc.code.lower()[:100], r=row["row_result_id"])  # fmt: skip
                continue
            await execute(conn, "UPDATE csv_import_row_results SET resulting_user_id = :u WHERE row_result_id = :r",
                          u=data["user_id"], r=row["row_result_id"])  # fmt: skip
            created += 1
            logins.append({"admission_id": r["admission_id"], "display_name": r["display_name"],
                           "username": data["username"], "email": r.get("email") or None,
                           "temporary_password": data["temporary_password"]})  # fmt: skip
        await execute(conn, "UPDATE csv_import_batches SET status = 'COMMITTED' WHERE batch_id = :b", b=batch_id)
        await audit.record(
            conn, ctx, action="STUDENT_IMPORT_COMMITTED", entity_type="csv_import_batch", entity_id=batch_id,
            school_id=live["school_id"], actor_user_id=actor.user_id, actor_role="SCHOOL_ADMIN", after={"created": created},
        )  # fmt: skip
        return 200, {"batch_id": batch_id, "created": created, "logins": logins}

    return await run_idempotent(request, actor, {"commit": str(batch_id)}, handler, school_id=batch["school_id"])


@router.get("/student-imports/{batch_id}")
async def get_import(batch_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    async with get_db().connect() as conn:
        batch = await fetch_one(conn, "SELECT * FROM csv_import_batches WHERE batch_id = :b", b=batch_id)
        if batch is None:
            raise errors.not_found("import", batch_id=str(batch_id))
        _require_admin(actor, request, batch["school_id"])
        rows = await fetch_all(
            conn, """SELECT row_number, validation_status, error_reason, raw_row_data ->> 'admission_id' AS admission_id,
                            raw_row_data ->> 'display_name' AS display_name, resulting_user_id
                       FROM csv_import_row_results WHERE batch_id = :b ORDER BY row_number""", b=batch_id,
        )  # fmt: skip
    return {"data": {**batch, "rows": rows}, "meta": {"request_id": request.state.request_id}}


# ------------------------------------------------------------------------------------------------
# Transfer and withdrawal (doc 21 Sept 9: history preserved; leaving students deactivated, never deleted)
# ------------------------------------------------------------------------------------------------
async def _student_enrollment(conn: AsyncConnection, student_id: UUID) -> dict:
    row = await fetch_one(
        conn,
        """SELECT se.*, sp.user_id FROM student_enrollments se JOIN student_profiles sp USING (student_id)
             JOIN academic_years ay ON ay.academic_year_id = se.academic_year_id AND ay.status = 'ACTIVE'
            WHERE se.student_id = :s AND se.status = 'ACTIVE' FOR UPDATE OF se""",
        s=student_id,
    )
    if row is None:
        raise errors.enrollment_inactive(student_id=str(student_id))
    return row


class TransferIn(BaseModel):
    section_id: UUID
    effective_date: date | None = None


@router.post("/students/{student_id}/transfer")
async def transfer(
    student_id: UUID, body: TransferIn, request: Request, actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async with get_db().connect() as conn:
        sp = await fetch_one(conn, "SELECT school_id FROM student_profiles WHERE student_id = :s", s=student_id)
    if sp is None:
        raise errors.not_found("student", student_id=str(student_id))
    _require_admin(actor, request, sp["school_id"])

    async def handler(conn: AsyncConnection):
        cur = await _student_enrollment(conn, student_id)
        target = await fetch_one(
            conn,
            """SELECT sec.section_id, sec.grade_id, g.academic_year_id, sec.name FROM sections sec JOIN grades g USING (grade_id)
                WHERE sec.section_id = :s AND sec.school_id = :sc""",
            s=body.section_id, sc=sp["school_id"],
        )  # fmt: skip
        if target is None:
            raise errors.not_found("section", section_id=str(body.section_id))
        if target["academic_year_id"] != cur["academic_year_id"]:
            raise errors.validation_error("Transfers stay within the academic year; use rollover for a new year.")
        if target["section_id"] == cur["section_id"]:
            raise errors.AppError("ALREADY_IN_SECTION", 409, "The student is already in this section.")
        when = body.effective_date or date.today()
        await execute(conn, "UPDATE student_enrollments SET status = 'TRANSFERRED', exit_date = :d WHERE enrollment_id = :e",
                      d=when, e=cur["enrollment_id"])  # fmt: skip
        new = await fetch_one(
            conn,
            """INSERT INTO student_enrollments (school_id, student_id, academic_year_id, grade_id, section_id,
                                                enrollment_date, status)
               VALUES (:s, :st, :y, :g, :sec, :d, 'ACTIVE') RETURNING enrollment_id""",
            s=sp["school_id"], st=student_id, y=cur["academic_year_id"], g=target["grade_id"], sec=target["section_id"], d=when,
        )  # fmt: skip
        await audit.record(
            conn, ctx, action="STUDENT_TRANSFERRED", entity_type="student_profile", entity_id=student_id,
            school_id=sp["school_id"], actor_user_id=actor.user_id, actor_role="SCHOOL_ADMIN",
            before={"section_id": cur["section_id"]}, after={"section_id": target["section_id"], "effective": when},
        )  # fmt: skip
        # Same event as a new student: current/future section work is back-filled (contract §11);
        # in-progress work from the old section continues by default (doc 21).
        await emit(
            conn, ctx, event_type="StudentEnrollmentActivated", aggregate_type="student_enrollment",
            aggregate_id=new["enrollment_id"], school_id=sp["school_id"],
            data={"student_id": student_id, "section_id": target["section_id"], "academic_year_id": cur["academic_year_id"],
                  "effective_at": when, "transferred_from": cur["section_id"]},
        )  # fmt: skip
        await _backfill(conn, ctx, sp["school_id"], student_id, target["section_id"], new["enrollment_id"])
        return 200, {"student_id": student_id, "enrollment_id": new["enrollment_id"], "section_id": target["section_id"],
                     "section_name": target["name"]}  # fmt: skip

    return await run_idempotent(request, actor, body, handler, school_id=sp["school_id"])


async def _backfill(conn: AsyncConnection, ctx: RequestContext, school_id: UUID, student_id: UUID, section_id: UUID,
                    enrollment_id: UUID) -> None:  # fmt: skip
    await backfill_student(conn, ctx, school_id=school_id, student_id=student_id, section_id=section_id)


class WithdrawIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
    effective_date: date | None = None


@router.post("/students/{student_id}/withdraw")
async def withdraw(
    student_id: UUID, body: WithdrawIn, request: Request, actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async with get_db().connect() as conn:
        sp = await fetch_one(
            conn, "SELECT school_id, user_id FROM student_profiles WHERE student_id = :s", s=student_id
        )
    if sp is None:
        raise errors.not_found("student", student_id=str(student_id))
    _require_admin(actor, request, sp["school_id"])

    async def handler(conn: AsyncConnection):
        cur = await _student_enrollment(conn, student_id)
        when = body.effective_date or date.today()
        await execute(conn, "UPDATE student_enrollments SET status = 'WITHDRAWN', exit_date = :d WHERE enrollment_id = :e",
                      d=when, e=cur["enrollment_id"])  # fmt: skip
        await execute(conn, "UPDATE student_profiles SET status = 'INACTIVE' WHERE student_id = :s", s=student_id)
        await execute(
            conn,
            """UPDATE school_user_roles SET status = 'INACTIVE' WHERE school_id = :sc AND user_id = :u
                  AND role_id = (SELECT role_id FROM roles WHERE role_code = 'STUDENT')""",
            sc=sp["school_id"], u=sp["user_id"],
        )  # fmt: skip
        await audit.record(
            conn, ctx, action="STUDENT_WITHDRAWN", entity_type="student_profile", entity_id=student_id,
            school_id=sp["school_id"], actor_user_id=actor.user_id, actor_role="SCHOOL_ADMIN",
            after={"effective": when, "reason": body.reason},
        )  # fmt: skip
        await emit(conn, ctx, event_type="StudentWithdrawn", aggregate_type="student", aggregate_id=student_id,
                   school_id=sp["school_id"], data={"student_id": student_id, "effective_at": when})  # fmt: skip
        return 200, {"student_id": student_id, "status": "WITHDRAWN"}

    return await run_idempotent(request, actor, body, handler, school_id=sp["school_id"])


# ------------------------------------------------------------------------------------------------
# Academic-year rollover (docs 12/14 MVP)
# ------------------------------------------------------------------------------------------------
class MappingIn(BaseModel):
    from_section_id: UUID
    to_section_id: UUID | None = None  # None = these students finish (graduate) with the old year


class RolloverIn(BaseModel):
    to_academic_year_id: UUID
    mappings: list[MappingIn] = Field(default_factory=list, max_length=500)
    pending_work: Literal["CLOSE_WITH_YEAR"] = "CLOSE_WITH_YEAR"
    dry_run: bool = True


def _suffix(name: str) -> str:
    """'10A' -> 'a', 'Grade 10 - B' -> 'b', 'Section B' -> 'section b'."""
    s = re.sub(r"\d+", " ", name.lower())
    s = re.sub(r"\b(grade|class|std|year)\b", " ", s)
    return re.sub(r"[\s\-_/]+", " ", s).strip()


async def _plan(conn: AsyncConnection, school_id: UUID, from_y: dict, to_y: dict, body: RolloverIn) -> dict:
    old = await fetch_all(
        conn,
        """SELECT sec.section_id, sec.name, g.grade_level,
                  (SELECT count(*) FROM student_enrollments se WHERE se.section_id = sec.section_id AND se.status = 'ACTIVE') AS n
             FROM sections sec JOIN grades g USING (grade_id) WHERE g.academic_year_id = :y ORDER BY g.grade_level, sec.name""",
        y=from_y["academic_year_id"],
    )
    new = await fetch_all(
        conn,
        """SELECT sec.section_id, sec.name, sec.grade_id, g.grade_level FROM sections sec JOIN grades g USING (grade_id)
            WHERE g.academic_year_id = :y""",
        y=to_y["academic_year_id"],
    )
    new_by_id = {s["section_id"]: s for s in new}
    top_level = max((s["grade_level"] for s in old), default=0)
    overrides = {m.from_section_id: m.to_section_id for m in body.mappings}
    moves, problems = [], []
    for s in old:
        if s["section_id"] in overrides:
            to = overrides[s["section_id"]]
            if to is not None and to not in new_by_id:
                problems.append({"section": s["name"], "problem": "target section is not in the new year"})
                continue
        else:
            candidates = [
                n for n in new if n["grade_level"] == s["grade_level"] + 1 and _suffix(n["name"]) == _suffix(s["name"])
            ]
            to = candidates[0]["section_id"] if len(candidates) == 1 else None
            if to is None and s["grade_level"] < top_level:
                problems.append(
                    {"section": s["name"], "problem": "no matching section in the next grade; map it explicitly"}
                )
                continue
        moves.append({"from_section_id": s["section_id"], "from_section": s["name"], "students": s["n"],
                      "to_section_id": to, "to_section": new_by_id[to]["name"] if to else None,
                      "outcome": "PROMOTED" if to else "COMPLETED"})  # fmt: skip
    pending = await fetch_one(
        conn,
        """SELECT count(*) AS n FROM assignments a JOIN lessons l USING (lesson_id)
            WHERE l.academic_year_id = :y AND a.status IN ('DRAFT', 'SCHEDULED', 'PUBLISHED', 'ACTIVE')""",
        y=from_y["academic_year_id"],
    )
    return {"moves": moves, "problems": problems, "open_assignments_to_close": pending["n"]}


@router.post("/academic-years/{academic_year_id}/rollover")
async def rollover(
    academic_year_id: UUID, body: RolloverIn, request: Request, actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async with get_db().connect() as conn:
        from_y = await fetch_one(conn, "SELECT * FROM academic_years WHERE academic_year_id = :y", y=academic_year_id)
    if from_y is None:
        raise errors.not_found("academic_year", academic_year_id=str(academic_year_id))
    school_id = from_y["school_id"]
    _require_admin(actor, request, school_id)

    async def handler(conn: AsyncConnection):
        fy = await fetch_one(
            conn, "SELECT * FROM academic_years WHERE academic_year_id = :y FOR UPDATE", y=academic_year_id
        )
        to_y = await fetch_one(conn, "SELECT * FROM academic_years WHERE academic_year_id = :y AND school_id = :s FOR UPDATE",
                               y=body.to_academic_year_id, s=school_id)  # fmt: skip
        if to_y is None:
            raise errors.not_found("academic_year", academic_year_id=str(body.to_academic_year_id))
        if fy["status"] != "ACTIVE" or to_y["status"] != "PLANNED":
            raise errors.AppError("ROLLOVER_NOT_ALLOWED", 409, "Roll over from the ACTIVE year into a PLANNED year.",
                                  {"from_status": fy["status"], "to_status": to_y["status"]})  # fmt: skip
        if to_y["start_date"] <= fy["start_date"]:
            raise errors.validation_error("The new year must start after the current one.")
        plan = await _plan(conn, school_id, fy, to_y, body)
        if body.dry_run or plan["problems"]:
            return 200, {"dry_run": True, "can_apply": not plan["problems"], **plan}
        rec = await fetch_one(
            conn,
            """INSERT INTO academic_year_rollovers (school_id, from_academic_year_id, to_academic_year_id, initiated_by, status)
               VALUES (:s, :f, :t, :u, 'IN_PROGRESS') RETURNING rollover_id""",
            s=school_id, f=academic_year_id, t=to_y["academic_year_id"], u=actor.user_id,
        )  # fmt: skip
        moved = completed = 0
        for m in plan["moves"]:
            enrollments = await fetch_all(conn, "SELECT * FROM student_enrollments WHERE section_id = :s AND status = 'ACTIVE'",
                                          s=m["from_section_id"])  # fmt: skip
            target = None
            if m["to_section_id"]:
                target = await fetch_one(
                    conn, "SELECT grade_id FROM sections WHERE section_id = :s", s=m["to_section_id"]
                )
            for e in enrollments:
                await execute(conn, "UPDATE student_enrollments SET status = 'COMPLETED', exit_date = :d WHERE enrollment_id = :e",
                              d=fy["end_date"], e=e["enrollment_id"])  # fmt: skip
                if target is None:
                    completed += 1
                    continue
                new = await fetch_one(
                    conn,
                    """INSERT INTO student_enrollments (school_id, student_id, academic_year_id, grade_id, section_id,
                                                        enrollment_date, status)
                       VALUES (:s, :st, :y, :g, :sec, :d, 'ACTIVE')
                       ON CONFLICT DO NOTHING RETURNING enrollment_id""",
                    s=school_id, st=e["student_id"], y=to_y["academic_year_id"], g=target["grade_id"],
                    sec=m["to_section_id"], d=to_y["start_date"],
                )  # fmt: skip
                if new:
                    moved += 1
                    await backfill_student(conn, ctx, school_id=school_id, student_id=e["student_id"],
                                           section_id=m["to_section_id"])  # fmt: skip
                    await emit(
                        conn, ctx, event_type="StudentEnrollmentActivated", aggregate_type="student_enrollment",
                        aggregate_id=new["enrollment_id"], school_id=school_id,
                        data={"student_id": e["student_id"], "section_id": m["to_section_id"],
                              "academic_year_id": to_y["academic_year_id"], "effective_at": to_y["start_date"],
                              "rollover_id": rec["rollover_id"]},
                    )  # fmt: skip
        # Pending work closes with the year (doc 12 default); the old year becomes read-only history.
        closed = await execute(
            conn,
            """UPDATE assignments a SET status = 'CLOSED' FROM lessons l
                WHERE l.lesson_id = a.lesson_id AND l.academic_year_id = :y
                  AND a.status IN ('DRAFT', 'SCHEDULED', 'PUBLISHED', 'ACTIVE')""",
            y=academic_year_id,
        )
        await execute(
            conn, "UPDATE teacher_section_subject_assignments SET status = 'INACTIVE', effective_to = :d "
                  "WHERE academic_year_id = :y AND status = 'ACTIVE'", d=fy["end_date"], y=academic_year_id,
        )  # fmt: skip
        await execute(
            conn, "UPDATE academic_years SET status = 'CLOSED' WHERE academic_year_id = :y", y=academic_year_id
        )
        await execute(
            conn, "UPDATE academic_years SET status = 'ACTIVE' WHERE academic_year_id = :y", y=to_y["academic_year_id"]
        )
        await execute(conn, "UPDATE academic_year_rollovers SET status = 'COMPLETED', completed_at = now() WHERE rollover_id = :r",
                      r=rec["rollover_id"])  # fmt: skip
        summary = {"promoted": moved, "completed": completed, "assignments_closed": closed}
        await audit.record(
            conn, ctx, action="ACADEMIC_YEAR_ROLLED_OVER", entity_type="academic_year_rollover", entity_id=rec["rollover_id"],
            school_id=school_id, actor_user_id=actor.user_id, actor_role="SCHOOL_ADMIN", after=summary,
        )  # fmt: skip
        await emit(conn, ctx, event_type="AcademicYearRolledOver", aggregate_type="academic_year_rollover",
                   aggregate_id=rec["rollover_id"], school_id=school_id,
                   data={"from": academic_year_id, "to": to_y["academic_year_id"], **summary})  # fmt: skip
        return 200, {"dry_run": False, "rollover_id": rec["rollover_id"], **summary,
                     "at": datetime.now(UTC)}  # fmt: skip

    return await run_idempotent(request, actor, body, handler, school_id=school_id)
