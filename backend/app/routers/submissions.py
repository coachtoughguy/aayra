"""Deliverable submissions for assignments with completion_mode SUBMISSION_REQUIRED or BOTH.

Flow: ask for an upload URL per file -> upload directly to storage -> submit (note and/or files).
The server checks every file really exists under this assignment's own prefix before accepting.
Late work is accepted and labelled (doc 21: Submitted Late, no automatic mark deduction) unless the
assignment disallows late submission. Resubmission creates attempt 2, 3, ... (history is kept).
School submissions land in the responsible teacher's "Needs Your Attention" queue.
"""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app import audit, errors
from app.auth import Actor, current_actor
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one, get_db
from app.idempotency import OpClass, run_idempotent
from app.outbox import emit
from app.routers.learning import load_student_assignment, student_ctx
from app.services.assessment import maybe_complete_assignment
from app.services.teaching import load_lesson_for_teacher
from app.storage import get_storage, safe_file_name, submission_prefix

router = APIRouter(prefix="/v1", tags=["submissions"])

MAX_FILES = 10
MAX_FILE_BYTES = 100 * 1024 * 1024


class UploadIn(BaseModel):
    file_name: str = Field(min_length=1, max_length=200)
    mime_type: str = Field(min_length=3, max_length=100)


async def _submittable(conn: AsyncConnection, student_id: UUID, sa_id: UUID, *, lock: bool = False) -> dict:
    sa = await load_student_assignment(conn, student_id, sa_id, lock=lock)
    a = await fetch_one(
        conn,
        "SELECT completion_mode, allow_late_submission, due_at, title, source_type, created_by FROM assignments WHERE assignment_id = :a",
        a=sa["assignment_id"],
    )
    if a["completion_mode"] == "LEARNING_COMPLETION":
        raise errors.AppError("SUBMISSION_NOT_REQUIRED", 409, "This assignment doesn't take a submission.")
    return {**sa, **{f"a_{k}": v for k, v in a.items()}}


@router.post("/student-assignments/{student_assignment_id}/submission-uploads", status_code=201)
async def request_upload(
    student_assignment_id: UUID, body: UploadIn, request: Request, actor: Actor = Depends(current_actor)
) -> dict:
    sctx = await student_ctx(actor, request)
    async with get_db().connect() as conn:
        await _submittable(conn, sctx.student_id, student_assignment_id)
    key = (
        f"{submission_prefix(sctx.school_id, student_assignment_id)}{uuid4().hex[:12]}-{safe_file_name(body.file_name)}"
    )
    signed = await get_storage().signed_upload(key)
    return {
        "data": {
            "object_key": signed.object_key,
            "upload_url": signed.upload_url,
            "expires_in_seconds": signed.expires_in_seconds,
        },
        "meta": {"request_id": request.state.request_id},
    }


class AttachmentIn(BaseModel):
    object_key: str = Field(min_length=1, max_length=500)
    file_name: str = Field(min_length=1, max_length=200)
    mime_type: str | None = Field(default=None, max_length=100)


class SubmissionIn(BaseModel):
    student_note: str | None = Field(default=None, max_length=10_000)
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=MAX_FILES)

    @model_validator(mode="after")
    def _something(self):
        if not self.attachments and not (self.student_note and self.student_note.strip()):
            raise ValueError("a submission needs a note or at least one file")
        return self


@router.post("/student-assignments/{student_assignment_id}/submissions", status_code=201)
async def submit(
    student_assignment_id: UUID,
    body: SubmissionIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    sctx = await student_ctx(actor, request)
    prefix = submission_prefix(sctx.school_id, student_assignment_id)
    sizes: dict[str, int] = {}
    for att in body.attachments:  # verify uploads before opening the transaction
        if not att.object_key.startswith(prefix):
            raise errors.validation_error("Attachment was not uploaded for this assignment.", object_key=att.object_key)
        size = await get_storage().object_size(att.object_key)
        if size is None:
            raise errors.AppError(
                "UPLOAD_NOT_FOUND", 409, "A file hasn't finished uploading.", {"object_key": att.object_key}
            )
        if size > MAX_FILE_BYTES:
            raise errors.validation_error("File too large.", object_key=att.object_key)
        sizes[att.object_key] = size

    async def handler(conn: AsyncConnection):
        sa = await _submittable(conn, sctx.student_id, student_assignment_id, lock=True)
        due = sa["effective_due_at"] or sa["a_due_at"]
        late = due is not None and datetime.now(UTC) > due
        if late and not sa["a_allow_late_submission"]:
            raise errors.AppError("LATE_SUBMISSION_NOT_ALLOWED", 409, "The due date has passed for this assignment.")
        n = await fetch_one(
            conn,
            "SELECT coalesce(max(attempt_number), 0) + 1 AS n FROM assignment_submissions WHERE student_assignment_id = :sa",
            sa=student_assignment_id,
        )
        sub = await fetch_one(
            conn,
            """INSERT INTO assignment_submissions (student_assignment_id, attempt_number, submitted_at, status, student_note)
               VALUES (:sa, :n, now(), :st, :note) RETURNING submission_id, attempt_number, submitted_at, status""",
            sa=student_assignment_id,
            n=n["n"],
            st="SUBMITTED_LATE" if late else "SUBMITTED",
            note=body.student_note,
        )
        for att in body.attachments:
            await execute(
                conn,
                """INSERT INTO submission_attachments (submission_id, object_key, mime_type, file_name, file_size_bytes)
                   VALUES (:s, :k, :m, :f, :sz)""",
                s=sub["submission_id"],
                k=att.object_key,
                m=att.mime_type,
                f=att.file_name,
                sz=sizes[att.object_key],
            )
        if sa["a_source_type"] == "SCHOOL":
            teacher = await fetch_one(
                conn,
                """SELECT t.teacher_id FROM teacher_section_subject_assignments t
                     JOIN lessons l ON l.subject_id = t.subject_id AND l.lesson_id = :l
                    WHERE t.section_id = :sec AND t.status = 'ACTIVE'
                      AND t.assignment_role IN ('PRIMARY_SUBJECT_TEACHER', 'CO_TEACHER')
                    ORDER BY (t.assignment_role = 'PRIMARY_SUBJECT_TEACHER') DESC LIMIT 1""",
                l=sa["lesson_id"],
                sec=sctx.section_id,
            )
            if teacher:
                await execute(
                    conn,
                    """INSERT INTO teacher_review_items (school_id, teacher_id, student_id, assignment_id, submission_id,
                                                         reason_type, status, ai_evaluation_snapshot)
                       VALUES (:sc, :t, :st, :a, :s, 'TEACHER_REQUIRED', 'PENDING', CAST(:snap AS jsonb))""",
                    sc=sctx.school_id,
                    t=teacher["teacher_id"],
                    st=sctx.student_id,
                    a=sa["assignment_id"],
                    s=sub["submission_id"],
                    snap=json.dumps(
                        {
                            "kind": "SUBMISSION",
                            "attempt_number": sub["attempt_number"],
                            "late": late,
                            "files": len(body.attachments),
                        }
                    ),  # fmt: skip
                )
        else:  # parent/self: tell whoever assigned it
            await execute(
                conn,
                """INSERT INTO notifications (school_id, recipient_user_id, notification_type, title, priority,
                                              entity_type, entity_id, deep_link)
                   VALUES (:sc, :u, 'SUBMISSION_RECEIVED', :t, 'NORMAL', 'assignment_submission', :s, :link)""",
                sc=sctx.school_id,
                u=sa["a_created_by"],
                t=f"Submitted: {sa['a_title']}",
                s=sub["submission_id"],
                link=f"aayra://assignments/{sa['assignment_id']}/submissions",
            )
        status = await maybe_complete_assignment(conn, student_assignment_id)
        await emit(
            conn,
            ctx,
            event_type="SubmissionReceived",
            aggregate_type="assignment_submission",
            aggregate_id=sub["submission_id"],
            school_id=sctx.school_id,
            data={
                "student_assignment_id": student_assignment_id,
                "attempt_number": sub["attempt_number"],
                "late": late,
            },
        )
        return 201, {**sub, "is_late": late, "files": len(body.attachments), "assignment_status": status}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.EVIDENCE, school_id=sctx.school_id)


@router.get("/student-assignments/{student_assignment_id}/submissions")
async def my_submissions(student_assignment_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    sctx = await student_ctx(actor, request)
    async with get_db().connect() as conn:
        await load_student_assignment(conn, sctx.student_id, student_assignment_id)
        subs = await _with_files(
            conn, "WHERE s.student_assignment_id = :sa", for_student=True, sa=student_assignment_id
        )
    return {"data": {"submissions": subs}, "meta": {"request_id": request.state.request_id}}


def _num(v) -> float | None:
    return float(v) if v is not None else None


async def _with_files(conn: AsyncConnection, where: str, *, for_student: bool, **params) -> list[dict]:
    subs = await fetch_all(
        conn,
        f"""SELECT s.submission_id, s.student_assignment_id, s.attempt_number, s.submitted_at, s.status, s.student_note,
                   s.marks_awarded, s.marked_at, s.marks_released_at, a.max_marks, a.marks_visibility,
                   sa.student_id, u.display_name AS student_name
              FROM assignment_submissions s
              JOIN student_assignments sa USING (student_assignment_id)
              JOIN assignments a ON a.assignment_id = sa.assignment_id
              JOIN student_profiles sp ON sp.student_id = sa.student_id JOIN users u ON u.user_id = sp.user_id
              {where} ORDER BY u.display_name, s.attempt_number""",
        **params,
    )
    for s in subs:
        files = await fetch_all(
            conn,
            "SELECT object_key, file_name, mime_type, file_size_bytes FROM submission_attachments WHERE submission_id = :s",
            s=s["submission_id"],
        )
        for f in files:
            f["download_url"] = await get_storage().signed_download_url(f.pop("object_key"), 900)
        s["files"] = files
        awarded, max_marks = _num(s.pop("marks_awarded")), _num(s.pop("max_marks"))
        visibility, released = s.pop("marks_visibility"), s.pop("marks_released_at")
        marked_at = s.pop("marked_at")
        if for_student:
            # Only a released mark on a STUDENT_VISIBLE assignment; never class statistics or rank.
            show = max_marks is not None and visibility == "STUDENT_VISIBLE" and released is not None
            s["marks"] = {"awarded": awarded, "out_of": max_marks} if show else None
        else:
            s["marks"] = None if max_marks is None else {
                "awarded": awarded, "out_of": max_marks, "visibility": visibility,
                "marked_at": marked_at, "released_at": released,
            }  # fmt: skip
    return subs


@router.get("/assignments/{assignment_id}/submissions")
async def assignment_submissions(assignment_id: UUID, request: Request, actor: Actor = Depends(current_actor)) -> dict:
    """Teacher view: every submission for an assignment (alphabetical; never ranked)."""
    async with get_db().connect() as conn:
        a = await fetch_one(conn, "SELECT lesson_id FROM assignments WHERE assignment_id = :a", a=assignment_id)
        if a is None:
            raise errors.not_found("assignment", assignment_id=str(assignment_id))
        await load_lesson_for_teacher(conn, actor, a["lesson_id"])
        subs = await _with_files(conn, "WHERE sa.assignment_id = :a", for_student=False, a=assignment_id)
    return {"data": {"submissions": subs}, "meta": {"request_id": request.state.request_id}}


# ------------------------------------------------------------------------------------------------
# Marks (doc 12): optional per assignment; teacher-only or visible to the student once released.
# Marks never change mastery and are never ranked or compared.
# ------------------------------------------------------------------------------------------------
async def _submission_for_teacher(
    conn: AsyncConnection, actor: Actor, submission_id: UUID, *, lock: bool = False
) -> dict:
    row = await fetch_one(
        conn,
        f"""SELECT s.*, sa.student_id, sa.assignment_id, a.lesson_id, a.school_id, a.title, a.max_marks,
                   a.marks_visibility, sp.user_id AS student_user_id
              FROM assignment_submissions s JOIN student_assignments sa USING (student_assignment_id)
              JOIN assignments a ON a.assignment_id = sa.assignment_id
              JOIN student_profiles sp ON sp.student_id = sa.student_id
             WHERE s.submission_id = :s{" FOR UPDATE OF s" if lock else ""}""",
        s=submission_id,
    )
    if row is None:
        raise errors.not_found("submission", submission_id=str(submission_id))
    await load_lesson_for_teacher(conn, actor, row["lesson_id"])
    if row["max_marks"] is None:
        raise errors.AppError("MARKS_NOT_ENABLED", 409, "This assignment isn't marked.")
    return row


class MarksIn(BaseModel):
    marks_awarded: float = Field(ge=0, le=10_000)


@router.put("/submissions/{submission_id}/marks")
async def set_marks(
    submission_id: UUID, body: MarksIn, request: Request, actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async with get_db().connect() as conn:
        sub = await _submission_for_teacher(conn, actor, submission_id)
    request.state.school_id = str(sub["school_id"])

    async def handler(conn: AsyncConnection):
        before = await _submission_for_teacher(conn, actor, submission_id, lock=True)
        if body.marks_awarded > float(before["max_marks"]):
            raise errors.validation_error(f"Marks can't exceed {float(before['max_marks']):g}.", field="marks_awarded")
        row = await fetch_one(
            conn,
            """UPDATE assignment_submissions SET marks_awarded = :m, marked_by = :u, marked_at = now()
                WHERE submission_id = :s RETURNING marks_awarded, marked_at, marks_released_at""",
            m=body.marks_awarded,
            u=actor.user_id,
            s=submission_id,
        )
        # Marking a submission is reviewing it: close its "Needs Your Attention" item.
        await execute(
            conn,
            """UPDATE teacher_review_items SET status = 'CONFIRMED', reviewed_at = now()
                WHERE submission_id = :s AND status = 'PENDING'""",
            s=submission_id,
        )
        await audit.record(
            conn, ctx, action="SUBMISSION_MARKED", entity_type="assignment_submission", entity_id=submission_id,
            school_id=before["school_id"], actor_user_id=actor.user_id, actor_role="TEACHER",
            before={"marks_awarded": _num(before["marks_awarded"])}, after={"marks_awarded": body.marks_awarded},
        )  # fmt: skip
        await emit(
            conn, ctx, event_type="SubmissionMarked", aggregate_type="assignment_submission",
            aggregate_id=submission_id, school_id=before["school_id"],
            data={"assignment_id": before["assignment_id"], "student_id": before["student_id"],
                  "amended": before["marks_awarded"] is not None},
        )  # fmt: skip
        if (
            row["marks_released_at"]
            and before["marks_awarded"] is not None
            and before["marks_visibility"] == "STUDENT_VISIBLE"
        ):
            await _notify_marks(conn, before, amended=True)
        return 200, {"submission_id": submission_id, "marks_awarded": _num(row["marks_awarded"]),
                     "out_of": _num(before["max_marks"]), "marked_at": row["marked_at"],
                     "released_at": row["marks_released_at"]}  # fmt: skip

    return await run_idempotent(request, actor, body, handler, school_id=sub["school_id"])


async def _notify_marks(conn: AsyncConnection, sub: dict, *, amended: bool = False) -> None:
    await execute(
        conn,
        """INSERT INTO notifications (school_id, recipient_user_id, notification_type, title, priority, entity_type,
                                      entity_id, deep_link)
           VALUES (:sc, :u, 'MARKS_RELEASED', :t, 'NORMAL', 'assignment_submission', :s, :link)""",
        sc=sub["school_id"],
        u=sub["student_user_id"],
        t=f"{'Updated mark' if amended else 'Marks'}: {sub['title']}",
        s=sub["submission_id"],
        link=f"aayra://student/assignments/{sub['student_assignment_id']}/submissions",
    )


async def _release(conn: AsyncConnection, ctx: RequestContext, subs: list[dict]) -> list[UUID]:
    released = []
    for sub in subs:
        if sub["marks_awarded"] is None or sub["marks_released_at"] is not None:
            continue
        await execute(conn, "UPDATE assignment_submissions SET marks_released_at = now() WHERE submission_id = :s",
                      s=sub["submission_id"])  # fmt: skip
        await _notify_marks(conn, sub)
        released.append(sub["submission_id"])
    if released:
        await emit(
            conn, ctx, event_type="MarksReleased", aggregate_type="assignment", aggregate_id=subs[0]["assignment_id"],
            school_id=subs[0]["school_id"], data={"submission_ids": released},
        )  # fmt: skip
    return released


def _require_visible(sub: dict) -> None:
    if sub["marks_visibility"] != "STUDENT_VISIBLE":
        raise errors.AppError("MARKS_TEACHER_ONLY", 409, "Marks on this assignment are for teachers only.")


@router.post("/submissions/{submission_id}/marks/release")
async def release_marks(
    submission_id: UUID, request: Request, actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    async with get_db().connect() as conn:
        sub = await _submission_for_teacher(conn, actor, submission_id)
    request.state.school_id = str(sub["school_id"])

    async def handler(conn: AsyncConnection):
        live = await _submission_for_teacher(conn, actor, submission_id, lock=True)
        _require_visible(live)
        if live["marks_awarded"] is None:
            raise errors.AppError("NOT_MARKED", 409, "Mark this submission before releasing it.")
        if live["marks_released_at"] is not None:
            raise errors.AppError("MARKS_ALREADY_RELEASED", 409, "This mark was already released.")
        await _release(conn, ctx, [live])
        return 200, {"submission_id": submission_id, "released": True}

    return await run_idempotent(request, actor, {"release": str(submission_id)}, handler, school_id=sub["school_id"])


@router.post("/assignments/{assignment_id}/marks/release")
async def release_all_marks(
    assignment_id: UUID, request: Request, actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:  # fmt: skip
    """Release every marked, not-yet-released submission of an assignment at once."""
    async with get_db().connect() as conn:
        a = await fetch_one(conn, "SELECT * FROM assignments WHERE assignment_id = :a", a=assignment_id)
        if a is None:
            raise errors.not_found("assignment", assignment_id=str(assignment_id))
        await load_lesson_for_teacher(conn, actor, a["lesson_id"])
    if a["max_marks"] is None:
        raise errors.AppError("MARKS_NOT_ENABLED", 409, "This assignment isn't marked.")
    _require_visible(a)
    request.state.school_id = str(a["school_id"])

    async def handler(conn: AsyncConnection):
        subs = await fetch_all(
            conn,
            """SELECT s.*, sa.assignment_id, sa.student_id, a.school_id, a.title, sp.user_id AS student_user_id
                 FROM assignment_submissions s JOIN student_assignments sa USING (student_assignment_id)
                 JOIN assignments a ON a.assignment_id = sa.assignment_id
                 JOIN student_profiles sp ON sp.student_id = sa.student_id
                WHERE sa.assignment_id = :a ORDER BY s.submission_id FOR UPDATE OF s""",
            a=assignment_id,
        )
        released = await _release(conn, ctx, subs)
        return 200, {"assignment_id": assignment_id, "released": len(released),
                     "not_marked_yet": sum(1 for s in subs if s["marks_awarded"] is None)}  # fmt: skip

    return await run_idempotent(request, actor, {"release_all": str(assignment_id)}, handler, school_id=a["school_id"])
