"""Contract §6.1 — content upload, decoupled from lesson creation."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, model_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.auth import Actor, current_actor
from app.context import RequestContext, get_context
from app.db import execute, fetch_one, get_db
from app.idempotency import OpClass, run_idempotent
from app.outbox import emit
from app.services.pipeline import enqueue_item_processing
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

    @model_validator(mode="after")
    def _links(self):
        if self.content_type in LINK_TYPES and self.source_url is None:
            raise ValueError(f"{self.content_type} requires source_url")
        if self.content_type not in LINK_TYPES and self.source_url is not None:
            raise ValueError("source_url is only for YOUTUBE / WEB_LINK; files are uploaded")
        return self


def _item_view(row: dict) -> dict:
    uploaded = bool(row.get("object_key") or row.get("source_url"))
    return {
        "content_item_id": row["content_item_id"],
        "content_type": row["content_type"],
        "title": row["title"],
        "source_url": row["source_url"],
        "mime_type": row["mime_type"],
        "file_size_bytes": row["file_size_bytes"],
        "upload_status": "COMPLETED" if uploaded else "AWAITING_UPLOAD",
    }


@router.post("/content-items", status_code=201)
async def create_content_item(
    body: ContentItemIn,
    request: Request,
    actor: Actor = Depends(current_actor),
    ctx: RequestContext = Depends(get_context),
) -> JSONResponse:
    async with get_db().connect() as conn:
        teacher = await require_teacher(conn, actor)
    request.state.school_id = str(teacher.school_id)

    async def handler(conn: AsyncConnection):
        row = await fetch_one(
            conn,
            """INSERT INTO content_items (school_id, content_type, title, source_url, uploaded_by)
               VALUES (:s, :t, :title, :url, :u) RETURNING *""",
            s=teacher.school_id,
            t=body.content_type,
            title=body.title,
            url=str(body.source_url) if body.source_url else None,
            u=actor.user_id,
        )
        await execute(
            conn,
            """INSERT INTO content_ownership (content_item_id, owner_type, owner_user_id, owner_school_id, origin_type)
               VALUES (:c, 'TEACHER', :u, :s, 'SCHOOL_ASSIGNED')""",
            c=row["content_item_id"],
            u=actor.user_id,
            s=teacher.school_id,
        )
        data = _item_view(row)
        if body.content_type in LINK_TYPES:
            # Nothing to upload: a link is "uploaded" on creation and goes straight to processing.
            await enqueue_item_processing(conn, teacher.school_id, row["content_item_id"])
            await emit(
                conn,
                ctx,
                event_type="ContentUploadCompleted",
                aggregate_type="content_item",
                aggregate_id=row["content_item_id"],
                school_id=teacher.school_id,
                data={"content_item_id": row["content_item_id"], "content_type": body.content_type},
            )
            data["upload"] = None
        else:
            signed = await get_storage().signed_upload(object_key_for(teacher.school_id, row["content_item_id"]))
            data["upload"] = {
                "object_key": signed.object_key,
                "upload_url": signed.upload_url,
                "expires_in_seconds": signed.expires_in_seconds,
            }
        return 201, data

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT, school_id=teacher.school_id)


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
