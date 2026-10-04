"""Optimistic concurrency (contract §1): every write to a versioned resource carries the
client's `version`; a mismatch is `409 VERSION_CONFLICT` with the live version in details."""

import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.db import fetch_one

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


async def update_versioned(
    conn: AsyncConnection,
    table: str,
    pk_column: str,
    pk_value: UUID,
    expected_version: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    """UPDATE ... SET <values>, version = version + 1 WHERE pk = ? AND version = ? RETURNING *."""
    table, pk_column = _ident(table), _ident(pk_column)
    assignments = ", ".join(f"{_ident(col)} = :v_{col}" for col in values)
    set_clause = f"{assignments}, version = version + 1" if assignments else "version = version + 1"
    params = {f"v_{col}": val for col, val in values.items()}
    row = await fetch_one(
        conn,
        f"UPDATE {table} SET {set_clause} WHERE {pk_column} = :pk AND version = :expected RETURNING *",
        pk=pk_value,
        expected=expected_version,
        **params,
    )
    if row is not None:
        return row
    current = await fetch_one(conn, f"SELECT version FROM {table} WHERE {pk_column} = :pk", pk=pk_value)
    if current is None:
        raise errors.not_found(table, id=str(pk_value))
    raise errors.version_conflict(
        entity=table,
        id=str(pk_value),
        expected_version=expected_version,
        current_version=current["version"],
    )
