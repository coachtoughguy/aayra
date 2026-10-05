"""Database access: one async engine, explicit transactions, plain SQL via SQLAlchemy Core.

Every state change runs inside `async with db.begin() as conn:` so that business rows,
the version check, the outbox event and the idempotency record commit or roll back together.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import get_settings


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True, pool_size=5)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.begin() as conn:
            yield conn

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        """Read-only work; autocommits nothing, rolls back on exit."""
        async with self.engine.connect() as conn:
            yield conn

    async def dispose(self) -> None:
        await self.engine.dispose()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(get_settings().database_url)
    return _db


def set_db(db: Database | None) -> None:
    """Test hook: point the app at a different database."""
    global _db
    _db = db


async def fetch_one(conn: AsyncConnection, sql: str, **params: Any) -> dict[str, Any] | None:
    row = (await conn.execute(text(sql), params)).mappings().first()
    return dict(row) if row else None


async def fetch_all(conn: AsyncConnection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


async def execute(conn: AsyncConnection, sql: str, **params: Any) -> int:
    return (await conn.execute(text(sql), params)).rowcount


def constraint_name(exc: BaseException) -> str | None:
    """Name of the violated constraint/index behind a SQLAlchemy IntegrityError (asyncpg)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = getattr(cur, "constraint_name", None)
        if name:
            return name
        cur = getattr(cur, "orig", None) or cur.__cause__
    return None
