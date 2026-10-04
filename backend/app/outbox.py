"""Transactional outbox (contract §1 "Domain event envelope", §12).

`emit()` must be called with the SAME connection/transaction as the state change it describes,
so the event exists if and only if the change committed. A worker publishes PENDING rows later.
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncConnection

from app.context import RequestContext
from app.db import execute


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


async def emit(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    school_id: UUID | None,
    data: dict[str, Any],
    event_version: int = 1,
    causation_id: UUID | None = None,
) -> UUID:
    event_id = uuid4()
    occurred_at = datetime.now(UTC)
    envelope = {
        "event_id": str(event_id),
        "event_type": event_type,
        "event_version": event_version,
        "occurred_at": occurred_at.isoformat(),
        "correlation_id": str(ctx.correlation_id),
        "causation_id": str(causation_id or ctx.request_id),
        "aggregate_type": aggregate_type,
        "aggregate_id": str(aggregate_id),
        "school_id": str(school_id) if school_id else None,
        "data": _jsonable(data),
    }
    await execute(
        conn,
        """INSERT INTO domain_event_outbox
             (event_id, school_id, aggregate_type, aggregate_id, event_type, payload,
              occurred_at, status)
           VALUES (:id, :school, :agg_type, :agg_id, :type, CAST(:payload AS jsonb),
                   :occurred_at, 'PENDING')""",
        id=event_id,
        school=school_id,
        agg_type=aggregate_type,
        agg_id=aggregate_id,
        type=event_type,
        payload=json.dumps(envelope),
        occurred_at=occurred_at,
    )
    return event_id
