"""Idempotency-Key handling (contract §1 "Idempotency").

Stored key = "{actor_id}:{METHOD path}:{client key}" so two clients can never collide.
`request_fingerprint` (sha256 of the canonical JSON body) rejects same-key/different-body reuse.
The business transaction and the COMPLETED idempotency record commit together, so a replay
always returns exactly what the first successful call returned.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from enum import Enum
from typing import Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.auth import Actor
from app.db import execute, fetch_one, get_db

MAX_CLIENT_KEY_LEN = 100
# A request still IN_PROGRESS after this long is treated as abandoned (process died mid-call)
# and may be taken over by a retry carrying the same key and body.
ABANDONED_AFTER = timedelta(seconds=60)

Handler = Callable[[AsyncConnection], Awaitable[tuple[int, Any]]]


class OpClass(Enum):
    """Expiry windows by operation class (contract §1)."""

    EVIDENCE = timedelta(hours=48)  # assessment/review responses, focus-cycle end
    CONTENT = timedelta(days=7)  # content, lesson, publish
    DEFAULT = timedelta(hours=24)


def fingerprint(body: BaseModel | dict | None) -> str:
    if isinstance(body, BaseModel):
        payload: Any = body.model_dump(mode="json")
    else:
        payload = body or {}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def scoped_key(actor: Actor, request: Request, client_key: str) -> str:
    return f"{actor.user_id}:{request.method} {request.url.path}:{client_key}"


def _client_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "").strip()
    if not key:
        raise errors.validation_error("Idempotency-Key header is required.", header="Idempotency-Key")
    if len(key) > MAX_CLIENT_KEY_LEN or not key.isprintable():
        raise errors.validation_error("Idempotency-Key is too long or malformed.")
    return key


def _replay(snapshot: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        snapshot["body"],
        status_code=snapshot["status_code"],
        headers={"Idempotent-Replayed": "true"},
    )


async def run_idempotent(
    request: Request,
    actor: Actor,
    body: BaseModel | dict | None,
    handler: Handler,
    *,
    op_class: OpClass = OpClass.DEFAULT,
    school_id: UUID | None = None,
) -> JSONResponse:
    key = scoped_key(actor, request, _client_key(request))
    fp = fingerprint(body)
    db = get_db()

    # Phase 1: claim the key (short transaction of its own).
    async with db.begin() as conn:
        await execute(
            conn,
            "DELETE FROM idempotency_records WHERE idempotency_key = :k AND expires_at < now()",
            k=key,
        )
        claimed = await fetch_one(
            conn,
            """INSERT INTO idempotency_records
                 (idempotency_key, school_id, request_fingerprint, status, expires_at)
               VALUES (:k, :school, :fp, 'IN_PROGRESS', now() + CAST(:ttl AS interval))
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING idempotency_key""",
            k=key,
            school=school_id,
            fp=fp,
            ttl=op_class.value,
        )
        if claimed is None:
            existing = await fetch_one(
                conn,
                """SELECT request_fingerprint, status, response_snapshot,
                          created_at < now() - CAST(:abandoned AS interval) AS abandoned
                     FROM idempotency_records WHERE idempotency_key = :k FOR UPDATE""",
                k=key,
                abandoned=ABANDONED_AFTER,
            )
            assert existing is not None
            if existing["request_fingerprint"] != fp:
                raise errors.idempotency_conflict(reason="different_request_body")
            if existing["status"] == "COMPLETED":
                return _replay(existing["response_snapshot"])
            if not existing["abandoned"]:
                raise errors.idempotency_conflict(reason="request_in_progress")
            await execute(
                conn,
                """UPDATE idempotency_records
                      SET status = 'IN_PROGRESS', created_at = now(), expires_at = now() + CAST(:ttl AS interval)
                    WHERE idempotency_key = :k""",
                k=key,
                ttl=op_class.value,
            )

    # Phase 2: do the work and record the result atomically.
    try:
        async with db.begin() as conn:
            status_code, result = await handler(conn)
            response_body = {"data": result, "meta": {"request_id": request.state.request_id}}
            snapshot = {"status_code": status_code, "body": response_body}
            await execute(
                conn,
                """UPDATE idempotency_records
                      SET status = 'COMPLETED', response_snapshot = CAST(:snap AS jsonb)
                    WHERE idempotency_key = :k""",
                k=key,
                snap=json.dumps(snapshot, default=str),
            )
    except BaseException:
        # Business transaction rolled back; release the key so the client can retry.
        async with db.begin() as conn:
            await execute(
                conn,
                "DELETE FROM idempotency_records WHERE idempotency_key = :k AND status = 'IN_PROGRESS'",
                k=key,
            )
        raise
    return JSONResponse(json.loads(json.dumps(response_body, default=str)), status_code=status_code)
