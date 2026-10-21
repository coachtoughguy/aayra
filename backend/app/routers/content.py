"""Contract §6.1 — content upload, decoupled from lesson creation."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.ai.sources import MAX_TRANSCRIPT_CHARS, youtube_video_id
from app.auth import Actor, current_actor, resolve_student_context
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one, get_db
from app.idempotency import OpClass, run_idempotent
from app.outbox import emit
from app.services.pipeline import enqueue_item_processing, item_extraction_state
from app.services.teaching import require_teacher
from app.storage import get_storage, object_key_for

router = APIRouter(prefix="/v1", tags=["content"])

ContentType = Literal["YOUTUBE", "WEB_LINK", "PDF", "DOCUMENT", "VIDEO", "AUDIO", "IMAGE", "PHOTO", "OTHER"]
LINK_TYPES = {"YOUTUBE", "WEB_LINK"}
MAX_UPLOAD_BYTES = 500 * 1024 * 1024


class ContentItemIn(BaseModel):
    content_type: ContentType
    title: str | None = Field(default=None, max_length=300)
    source_url: HttpUrl | None = None
    # SCHOOL (teacher material, default), PARENT (a guardian's material for student_id), SELF (a student's own)
    purpose: Literal["SCHOOL", "PARENT", "SELF"] = "SCHOOL"
    student_id: UUID | None = None
    # YOUTUBE only: the video's transcript (pasted text, or the text of a .vtt/.srt file). It can
    # also be added later with POST /v1/content-items/{id}/transcript.
    transcript_text: str | None = Field(default=None, max_length=MAX_TRANSCRIPT_CHARS)

    @model_validator(mode="after")
    def _links(self):
        if self.content_type == "YOUTUBE" and self.source_url and not youtube_video_id(str(self.source_url)):
            raise ValueError("source_url must be a YouTube video link")
        if self.transcript_text is not None and self.content_type != "YOUTUBE":
            raise ValueError("transcript_text is only for YOUTUBE")
        if self.content_type in LINK_TYPES and self.source_url is None:
            raise ValueError(f"{self.content_type} requires source_url")
        if self.content_type not in LINK_TYPES and self.source_url is not None:
            raise ValueError("source_url is only for YOUTUBE / WEB_LINK; files are uploaded")
        if (self.purpose == "PARENT") != (self.student_id is not None):
            raise ValueError("student_id is required for PARENT content and only allowed there")
        return self


OWNERSHIP = {
    "SCHOOL": ("TEACHER", "SCHOOL_ASSIGNED"),
    "PARENT": ("PARENT", "PARENT_ASSIGNED"),
    "SELF": ("STUDENT", "SELF_LEARNING"),
}


async def _owner_school(conn: AsyncConnection, actor: Actor, body: "ContentItemIn") -> UUID:
    if body.purpose == "SCHOOL":
        return (await require_teacher(conn, actor)).school_id
    if body.purpose == "PARENT":
        from app.routers.family_learning import require_guardian_of

        return (await require_guardian_of(conn, actor, body.student_id))["school_id"]
    return (await resolve_student_context(conn, actor)).school_id


def is_uploaded(item: dict) -> bool:
    """Ready to attach to a lesson: files are uploaded, web links exist, YouTube links have a transcript."""
    if item["content_type"] == "YOUTUBE":
        return bool(item.get("object_key"))
    return bool(item.get("object_key") or item.get("source_url"))


def _upload_status(row: dict) -> str:
    if is_uploaded(row):
        return "COMPLETED"
    return "AWAITING_TRANSCRIPT" if row["content_type"] == "YOUTUBE" else "AWAITING_UPLOAD"


async def _store_transcript(school_id: UUID, content_item_id: UUID, text: str) -> str:
    key = object_key_for(school_id, content_item_id)
    await get_storage().upload(key, text.encode("utf-8"), "text/plain; charset=utf-8")
    return key


def _item_view(row: dict) -> dict:
    return {
        "content_item_id": row["content_item_id"],
        "content_type": row["content_type"],
        "title": row["title"],
        "source_url": row["source_url"],
        "mime_type": row["mime_type"],
        "file_size_bytes": row["file_size_bytes"],
        "upload_status": _upload_status(row),
    }


@router.post("/content-items", status_code=201)
async def create_content_item(
    body: ContentItemIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        school_id = await _owner_school(conn, actor, body)
    request.state.school_id = str(school_id)
    owner_type, origin_type = OWNERSHIP[body.purpose]

    async def handler(conn: AsyncConnection):
        row = await fetch_one(
            conn,
            """INSERT INTO content_items (school_id, content_type, title, source_url, uploaded_by)
               VALUES (:s, :t, :title, :url, :u) RETURNING *""",
            s=school_id,
            t=body.content_type,
            title=body.title,
            url=str(body.source_url) if body.source_url else None,
            u=actor.user_id,
        )
        await execute(
            conn,
            """INSERT INTO content_ownership (content_item_id, owner_type, owner_user_id, owner_school_id, origin_type)
               VALUES (:c, :ot, :u, :s, :origin)""",
            c=row["content_item_id"],
            ot=owner_type,
            u=actor.user_id,
            s=school_id,
            origin=origin_type,
        )
        if body.content_type == "YOUTUBE" and body.transcript_text and body.transcript_text.strip():
            key = await _store_transcript(school_id, row["content_item_id"], body.transcript_text)
            row = await fetch_one(
                conn, """UPDATE content_items SET object_key = :k, mime_type = 'text/plain',
                                file_size_bytes = :sz WHERE content_item_id = :c RETURNING *""",
                k=key, sz=len(body.transcript_text.encode()), c=row["content_item_id"],
            )  # fmt: skip
        data = _item_view(row)
        if body.content_type == "YOUTUBE" and not is_uploaded(row):
            data["upload"] = None  # waits for POST /v1/content-items/{id}/transcript
        elif body.content_type in LINK_TYPES:
            # Nothing to upload: a link is "uploaded" on creation and goes straight to processing.
            await enqueue_item_processing(conn, school_id, row["content_item_id"])
            await emit(
                conn,
                ctx,
                event_type="ContentUploadCompleted",
                aggregate_type="content_item",
                aggregate_id=row["content_item_id"],
                school_id=school_id,
                data={"content_item_id": row["content_item_id"], "content_type": body.content_type},
            )
            data["upload"] = None
        else:
            signed = await get_storage().signed_upload(object_key_for(school_id, row["content_item_id"]))
            data["upload"] = {
                "object_key": signed.object_key,
                "upload_url": signed.upload_url,
                "expires_in_seconds": signed.expires_in_seconds,
            }
        return 201, data

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT, school_id=school_id)


class CompleteUploadIn(BaseModel):
    object_key: str = Field(min_length=1, max_length=500)
    mime_type: str = Field(min_length=3, max_length=100)
    file_size_bytes: int = Field(gt=0, le=MAX_UPLOAD_BYTES)
    duration_seconds: int | None = Field(default=None, ge=0)
    page_count: int | None = Field(default=None, ge=0)


@router.post("/content-items/{content_item_id}/complete-upload")
async def complete_upload(
    content_item_id: UUID,
    body: CompleteUploadIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        item = await fetch_one(conn, "SELECT * FROM content_items WHERE content_item_id = :c", c=content_item_id)
    if item is None:
        raise errors.not_found("content_item", content_item_id=str(content_item_id))
    if item["uploaded_by"] != actor.user_id:
        raise errors.insufficient_role_scope(reason="not_uploader")
    request.state.school_id = str(item["school_id"])
    if item["content_type"] in LINK_TYPES:
        raise errors.AppError("UPLOAD_NOT_APPLICABLE", 409, "Links are not uploaded.")
    expected_key = object_key_for(item["school_id"], content_item_id)
    if body.object_key != expected_key:
        raise errors.validation_error("object_key does not match the issued upload location.", field="object_key")

    async def handler(conn: AsyncConnection):
        locked = await fetch_one(
            conn, "SELECT object_key FROM content_items WHERE content_item_id = :c FOR UPDATE", c=content_item_id
        )
        if locked["object_key"]:
            raise errors.AppError("UPLOAD_ALREADY_COMPLETED", 409, "This upload was already completed.")
        size = await get_storage().object_size(body.object_key)
        if size is None:
            raise errors.AppError(
                "UPLOAD_NOT_FOUND", 409, "No uploaded file was found at object_key; upload it first, then retry."
            )
        row = await fetch_one(
            conn,
            """UPDATE content_items SET object_key = :k, mime_type = :m, file_size_bytes = :sz,
                      duration_seconds = :d, page_count = :p
                WHERE content_item_id = :c RETURNING *""",
            k=body.object_key,
            m=body.mime_type,
            sz=body.file_size_bytes,
            d=body.duration_seconds,
            p=body.page_count,
            c=content_item_id,
        )
        await enqueue_item_processing(conn, item["school_id"], content_item_id)
        await emit(
            conn,
            ctx,
            event_type="ContentUploadCompleted",
            aggregate_type="content_item",
            aggregate_id=content_item_id,
            school_id=item["school_id"],
            data={"content_item_id": content_item_id, "content_type": item["content_type"]},
        )
        return 200, _item_view(row)

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT, school_id=item["school_id"])


# ------------------------------------------------------------------------------------------------
# POST /v1/content-items/{id}/retry-processing — retry a file whose processing failed
# ------------------------------------------------------------------------------------------------
@router.post("/content-items/{content_item_id}/retry-processing")
async def retry_processing(
    content_item_id: UUID,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        item = await fetch_one(conn, "SELECT * FROM content_items WHERE content_item_id = :c", c=content_item_id)
        if item is None:
            raise errors.not_found("content_item", content_item_id=str(content_item_id))
        if item["uploaded_by"] != actor.user_id:  # the uploader (teacher, parent or student) can always retry
            teacher = await require_teacher(conn, actor)
            if teacher.school_id != item["school_id"]:
                raise errors.not_found("content_item", content_item_id=str(content_item_id))
    request.state.school_id = str(item["school_id"])

    async def handler(conn: AsyncConnection):
        # Lock the item so two retries can't both enqueue.
        await fetch_one(
            conn, "SELECT 1 AS x FROM content_items WHERE content_item_id = :c FOR UPDATE", c=content_item_id
        )
        state = await item_extraction_state(conn, content_item_id)
        if state not in ("FAILED", "CANCELLED"):
            raise errors.AppError(
                "RETRY_NOT_ALLOWED",
                409,
                "Only a file whose processing failed can be retried.",
                {"processing_status": state},
            )
        await enqueue_item_processing(conn, item["school_id"], content_item_id)
        lessons = await fetch_all(
            conn,
            """UPDATE lessons l SET status = 'PROCESSING', version = l.version + 1
                 FROM lesson_content lc
                WHERE lc.lesson_id = l.lesson_id AND lc.content_item_id = :c AND l.status = 'PROCESSING_FAILED'
                RETURNING l.lesson_id""",
            c=content_item_id,
        )
        await emit(
            conn,
            ctx,
            event_type="ContentProcessingRetried",
            aggregate_type="content_item",
            aggregate_id=content_item_id,
            school_id=item["school_id"],
            data={"content_item_id": content_item_id, "lesson_ids": [r["lesson_id"] for r in lessons]},
        )
        return 200, {
            "content_item_id": content_item_id,
            "processing_status": "QUEUED",
            "lessons_reprocessing": [r["lesson_id"] for r in lessons],
        }

    return await run_idempotent(
        request, actor, {"retry": str(content_item_id)}, handler, op_class=OpClass.CONTENT, school_id=item["school_id"]
    )


# ------------------------------------------------------------------------------------------------
# POST /v1/content-items/{id}/transcript — the transcript for a YouTube link (Oct 20 decision)
# ------------------------------------------------------------------------------------------------
class TranscriptIn(BaseModel):
    text: str = Field(min_length=20, max_length=MAX_TRANSCRIPT_CHARS)


@router.post("/content-items/{content_item_id}/transcript")
async def provide_transcript(
    content_item_id: UUID,
    body: TranscriptIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    """Paste a transcript (or send the text of a .vtt/.srt file). Allowed once, or again to replace a
    transcript whose processing failed."""
    async with get_db().connect() as conn:
        item = await fetch_one(conn, "SELECT * FROM content_items WHERE content_item_id = :c", c=content_item_id)
    if item is None:
        raise errors.not_found("content_item", content_item_id=str(content_item_id))
    if item["uploaded_by"] != actor.user_id:
        raise errors.insufficient_role_scope(reason="not_uploader")
    if item["content_type"] != "YOUTUBE":
        raise errors.AppError("TRANSCRIPT_NOT_APPLICABLE", 409, "Only YouTube links take a transcript.")
    request.state.school_id = str(item["school_id"])

    async def handler(conn: AsyncConnection):
        locked = await fetch_one(
            conn, "SELECT object_key FROM content_items WHERE content_item_id = :c FOR UPDATE", c=content_item_id
        )
        state = await item_extraction_state(conn, content_item_id)
        if locked["object_key"] and state not in ("FAILED", "CANCELLED"):
            raise errors.AppError(
                "TRANSCRIPT_ALREADY_PROVIDED", 409, "This video already has a transcript.", {"processing_status": state}
            )
        key = await _store_transcript(item["school_id"], content_item_id, body.text)
        row = await fetch_one(
            conn, """UPDATE content_items SET object_key = :k, mime_type = 'text/plain', file_size_bytes = :sz
                      WHERE content_item_id = :c RETURNING *""",
            k=key, sz=len(body.text.encode()), c=content_item_id,
        )  # fmt: skip
        await execute(conn, "DELETE FROM content_evidence_chunks WHERE content_item_id = :c", c=content_item_id)
        await enqueue_item_processing(conn, item["school_id"], content_item_id)
        await execute(
            conn,
            """UPDATE lessons l SET status = 'PROCESSING', version = l.version + 1 FROM lesson_content lc
                WHERE lc.lesson_id = l.lesson_id AND lc.content_item_id = :c AND l.status = 'PROCESSING_FAILED'""",
            c=content_item_id,
        )
        await emit(
            conn, ctx, event_type="ContentUploadCompleted", aggregate_type="content_item",
            aggregate_id=content_item_id, school_id=item["school_id"],
            data={"content_item_id": content_item_id, "content_type": "YOUTUBE", "transcript": "PROVIDED"},
        )  # fmt: skip
        return 200, _item_view(row)

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT, school_id=item["school_id"])
