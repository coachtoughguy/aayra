"""audit_events writer (insert-only ledger, contract §1). Called in the same transaction as the
change it records."""

import json
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app.context import RequestContext
from app.db import execute

ROLE_IDS = {"STUDENT": 1, "TEACHER": 2, "PARENT_GUARDIAN": 3, "SCHOOL_ADMIN": 4, "PRINCIPAL": 5}


async def record(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID,
    school_id: UUID | None,
    actor_user_id: UUID | None,
    actor_role: str | None,
    after: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
) -> None:
    await execute(
        conn,
        """INSERT INTO audit_events (school_id, actor_user_id, actor_role, action, entity_type,
                                     entity_id, before_data, after_data, request_id, occurred_at)
           VALUES (:school, :actor, :role, :action, :etype, :eid,
                   CAST(:before AS jsonb), CAST(:after AS jsonb), :rid, now())""",
        school=school_id,
        actor=actor_user_id,
        role=ROLE_IDS.get(actor_role or ""),
        action=action,
        etype=entity_type,
        eid=entity_id,
        before=json.dumps(before, default=str) if before is not None else None,
        after=json.dumps(after, default=str) if after is not None else None,
        rid=ctx.request_id,
    )
