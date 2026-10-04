"""Test harness.

Once per session: build `aayra_template` = Supabase-compat shim + every migration in
/supabase/migrations (the exact SQL that is live in production).
Per test: CREATE DATABASE ... TEMPLATE aayra_template, point the app at it, drop it afterwards.
Every test therefore starts from a pristine, production-identical schema.
"""

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import httpx
import pytest

from app.db import Database, set_db
from app.main import create_app

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = sorted((REPO / "supabase" / "migrations").glob("*.sql"))
COMPAT = REPO / "supabase" / "local" / "00_supabase_compat.sql"

PG_HOST = os.environ.get("AAYRA_TEST_PGHOST", "/tmp")
PG_PORT = int(os.environ.get("AAYRA_TEST_PGPORT", "54322"))
PG_USER = os.environ.get("AAYRA_TEST_PGUSER", "postgres")
PG_PASSWORD = os.environ.get("AAYRA_TEST_PGPASSWORD") or None
TEMPLATE = "aayra_template"


async def _admin(database: str = "postgres") -> asyncpg.Connection:
    return await asyncpg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=database)


def _sqlalchemy_url(database: str) -> str:
    auth = PG_USER + (f":{PG_PASSWORD}" if PG_PASSWORD else "")
    if PG_HOST.startswith("/"):
        return f"postgresql+asyncpg://{auth}@/{database}?host={PG_HOST}&port={PG_PORT}"
    return f"postgresql+asyncpg://{auth}@{PG_HOST}:{PG_PORT}/{database}"


@pytest.fixture(scope="session", autouse=True)
async def template_database() -> AsyncIterator[str]:
    admin = await _admin()
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {TEMPLATE}")
        await admin.execute(f"CREATE DATABASE {TEMPLATE}")
    finally:
        await admin.close()
    conn = await _admin(TEMPLATE)
    try:
        await conn.execute(COMPAT.read_text())
        for path in MIGRATIONS:
            try:
                async with conn.transaction():
                    await conn.execute(path.read_text())
            except Exception as exc:  # pragma: no cover - surfaces migration drift loudly
                raise RuntimeError(f"migration failed: {path.name}: {exc}") from exc
    finally:
        await conn.close()
    yield TEMPLATE


@pytest.fixture
async def db(template_database: str) -> AsyncIterator[Database]:
    name = f"aayra_test_{uuid.uuid4().hex[:12]}"
    admin = await _admin()
    try:
        await admin.execute(f"CREATE DATABASE {name} TEMPLATE {template_database}")
    finally:
        await admin.close()
    database = Database(_sqlalchemy_url(name))
    set_db(database)
    try:
        yield database
    finally:
        await database.dispose()
        set_db(None)
        admin = await _admin()
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        finally:
            await admin.close()


@pytest.fixture
def app(db: Database):
    return create_app()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
