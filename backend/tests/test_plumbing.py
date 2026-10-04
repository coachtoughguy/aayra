"""Shared plumbing every endpoint relies on: auth, error envelope, idempotency, optimistic
concurrency, transactional outbox, request context, student tenancy.

A tiny test-only router (`/_test/widgets`) exercises the helpers exactly the way real
endpoints will, against a scratch table created in each test database.
"""

import uuid
from uuid import UUID

import pytest
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app import errors
from app.auth import Actor, current_actor, resolve_student_context
from app.concurrency import update_versioned
from app.context import RequestContext, get_context
from app.db import execute, fetch_all, fetch_one
from app.idempotency import OpClass, run_idempotent
from app.outbox import emit
from tests.factories import (
    auth,
    create_school,
    create_student,
    create_user,
    create_year_grade_section,
    token_for,
)


class WidgetIn(BaseModel):
    name: str
    fail: bool = False


class WidgetPatch(BaseModel):
    name: str
    version: int


widgets_router = APIRouter()


@widgets_router.post("/_test/widgets", status_code=201)
async def create_widget(
    body: WidgetIn, request: Request, actor: Actor = Depends(current_actor), ctx: RequestContext = Depends(get_context)
):
    async def handler(conn):
        widget_id = uuid.uuid4()
        await execute(conn, "INSERT INTO _test_widgets (id, name) VALUES (:id, :n)", id=widget_id, n=body.name)
        await emit(
            conn,
            ctx,
            event_type="WidgetCreated",
            aggregate_type="widget",
            aggregate_id=widget_id,
            school_id=None,
            data={"name": body.name},
        )
        if body.fail:
            raise errors.AppError("BOOM", 409, "Deliberate failure after writes.")
        return 201, {"id": str(widget_id), "name": body.name, "version": 1}

    return await run_idempotent(request, actor, body, handler, op_class=OpClass.CONTENT)


@widgets_router.patch("/_test/widgets/{widget_id}")
async def patch_widget(widget_id: UUID, body: WidgetPatch, actor: Actor = Depends(current_actor)):
    from app.db import get_db

    async with get_db().begin() as conn:
        row = await update_versioned(conn, "_test_widgets", "id", widget_id, body.version, {"name": body.name})
    return {"data": {"id": str(row["id"]), "name": row["name"], "version": row["version"]}}


@pytest.fixture
def app(db):
    from app.main import create_app

    application = create_app()
    application.include_router(widgets_router)
    return application


@pytest.fixture
async def teacher(db) -> UUID:
    async with db.begin() as conn:
        await execute(
            conn,
            "CREATE TABLE _test_widgets (id uuid PRIMARY KEY, name text NOT NULL, version integer NOT NULL DEFAULT 1)",
        )
        school = await create_school(conn)
        return await create_user(conn, "Sarah", school_id=school, roles=("TEACHER",))


# --- request context & errors ------------------------------------------------------------------


async def test_healthz_reports_seeded_roles_and_request_id(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["data"] == {"status": "ok", "roles": 5}
    UUID(r.headers["X-Request-Id"])


async def test_correlation_id_is_propagated_from_header(client, teacher):
    corr = str(uuid.uuid4())
    r = await client.get("/v1/me", headers=auth(teacher, **{"X-Correlation-Id": corr}))
    assert r.headers["X-Correlation-Id"] == corr
    assert r.headers["X-Request-Id"] != corr


# --- authentication ----------------------------------------------------------------------------


async def test_missing_token_is_401_with_stable_envelope(client):
    r = await client.get("/v1/me")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert body["meta"]["request_id"] == r.headers["X-Request-Id"]


@pytest.mark.parametrize("bad", ["wrong-secret", "expired", "wrong-audience", "garbage"])
async def test_invalid_tokens_are_rejected(client, teacher, bad):
    token = {
        "wrong-secret": token_for(teacher, secret="x" * 40),
        "expired": token_for(teacher, expires_in=-10),
        "wrong-audience": token_for(teacher, audience="anon"),
        "garbage": "not.a.jwt",
    }[bad]
    r = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_valid_token_for_unknown_user_is_401(client, teacher):
    r = await client.get("/v1/me", headers=auth(uuid.uuid4()))
    assert r.status_code == 401


async def test_inactive_account_is_403(client, db):
    async with db.begin() as conn:
        user = await create_user(conn, status="LOCKED")
    r = await client.get("/v1/me", headers=auth(user))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ACCOUNT_INACTIVE"


async def test_me_lists_active_roles_per_school(client, db):
    async with db.begin() as conn:
        school = await create_school(conn)
        user = await create_user(conn, "Multi", school_id=school, roles=("TEACHER", "PRINCIPAL"))
        await execute(
            conn,
            """UPDATE school_user_roles SET status='INACTIVE'
                               WHERE user_id=:u AND role_id=5""",
            u=user,
        )
    r = await client.get("/v1/me", headers=auth(user))
    assert r.status_code == 200
    assert r.json()["data"]["roles"] == {str(school): ["TEACHER"]}


# --- idempotency -------------------------------------------------------------------------------


async def _counts(db) -> tuple[int, int]:
    async with db.connect() as conn:
        w = await fetch_one(conn, "SELECT count(*) AS n FROM _test_widgets")
        e = await fetch_one(conn, "SELECT count(*) AS n FROM domain_event_outbox")
    return w["n"], e["n"]


async def test_same_key_same_body_replays_without_rewriting(client, db, teacher):
    h = auth(teacher, **{"Idempotency-Key": "k-1"})
    first = await client.post("/_test/widgets", json={"name": "a"}, headers=h)
    second = await client.post("/_test/widgets", json={"name": "a"}, headers=h)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert await _counts(db) == (1, 1)


async def test_same_key_different_body_is_conflict(client, db, teacher):
    h = auth(teacher, **{"Idempotency-Key": "k-2"})
    await client.post("/_test/widgets", json={"name": "a"}, headers=h)
    r = await client.post("/_test/widgets", json={"name": "b"}, headers=h)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert r.json()["error"]["details"]["reason"] == "different_request_body"
    assert await _counts(db) == (1, 1)


async def test_same_client_key_is_scoped_per_actor(client, db, teacher):
    async with db.begin() as conn:
        other = await create_user(conn, "Other")
    await client.post("/_test/widgets", json={"name": "a"}, headers=auth(teacher, **{"Idempotency-Key": "shared"}))
    r = await client.post("/_test/widgets", json={"name": "a"}, headers=auth(other, **{"Idempotency-Key": "shared"}))
    assert r.status_code == 201
    assert "Idempotent-Replayed" not in r.headers
    assert await _counts(db) == (2, 2)


async def test_missing_idempotency_key_is_rejected(client, teacher):
    r = await client.post("/_test/widgets", json={"name": "a"}, headers=auth(teacher))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_failure_rolls_back_everything_and_releases_key(client, db, teacher):
    h = auth(teacher, **{"Idempotency-Key": "k-3"})
    r = await client.post("/_test/widgets", json={"name": "a", "fail": True}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "BOOM"
    # Neither the business row nor its outbox event survived the rollback.
    assert await _counts(db) == (0, 0)
    async with db.connect() as conn:
        assert await fetch_one(conn, "SELECT 1 AS x FROM idempotency_records") is None


async def test_abandoned_in_progress_key_can_be_taken_over(client, db, teacher):
    h = auth(teacher, **{"Idempotency-Key": "k-4"})
    from app.idempotency import fingerprint

    key = f"{teacher}:POST /_test/widgets:k-4"
    async with db.begin() as conn:  # simulate a process that died mid-request 5 minutes ago
        await execute(
            conn,
            """INSERT INTO idempotency_records
            (idempotency_key, request_fingerprint, status, created_at, expires_at)
            VALUES (:k, :fp, 'IN_PROGRESS', now() - interval '5 minutes', now() + interval '1 day')""",
            k=key,
            fp=fingerprint(WidgetIn(name="a")),
        )
    r = await client.post("/_test/widgets", json={"name": "a"}, headers=h)
    assert r.status_code == 201
    assert await _counts(db) == (1, 1)


async def test_recent_in_progress_key_is_conflict(client, db, teacher):
    from app.idempotency import fingerprint

    key = f"{teacher}:POST /_test/widgets:k-5"
    async with db.begin() as conn:
        await execute(
            conn,
            """INSERT INTO idempotency_records
            (idempotency_key, request_fingerprint, status, expires_at)
            VALUES (:k, :fp, 'IN_PROGRESS', now() + interval '1 day')""",
            k=key,
            fp=fingerprint(WidgetIn(name="a")),
        )
    r = await client.post("/_test/widgets", json={"name": "a"}, headers=auth(teacher, **{"Idempotency-Key": "k-5"}))
    assert r.status_code == 409
    assert r.json()["error"]["details"]["reason"] == "request_in_progress"


async def test_idempotency_record_expiry_uses_operation_class(client, db, teacher):
    await client.post("/_test/widgets", json={"name": "a"}, headers=auth(teacher, **{"Idempotency-Key": "k-6"}))
    async with db.connect() as conn:
        row = await fetch_one(
            conn,
            """SELECT round(extract(epoch FROM expires_at - created_at)
                                       / 3600) AS hours FROM idempotency_records""",
        )
    assert row["hours"] == 7 * 24  # OpClass.CONTENT


# --- optimistic concurrency --------------------------------------------------------------------


async def test_versioned_update_and_stale_write(client, db, teacher):
    created = await client.post(
        "/_test/widgets", json={"name": "a"}, headers=auth(teacher, **{"Idempotency-Key": "k-7"})
    )
    wid = created.json()["data"]["id"]
    ok = await client.patch(f"/_test/widgets/{wid}", json={"name": "b", "version": 1}, headers=auth(teacher))
    assert ok.status_code == 200 and ok.json()["data"]["version"] == 2
    stale = await client.patch(f"/_test/widgets/{wid}", json={"name": "c", "version": 1}, headers=auth(teacher))
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "VERSION_CONFLICT"
    assert err["details"]["current_version"] == 2 and err["details"]["expected_version"] == 1


async def test_versioned_update_of_missing_row_is_404(client, teacher):
    r = await client.patch(f"/_test/widgets/{uuid.uuid4()}", json={"name": "x", "version": 1}, headers=auth(teacher))
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


# --- outbox ------------------------------------------------------------------------------------


async def test_outbox_event_carries_versioned_envelope(client, db, teacher):
    corr = str(uuid.uuid4())
    r = await client.post(
        "/_test/widgets",
        json={"name": "a"},
        headers=auth(teacher, **{"Idempotency-Key": "k-8", "X-Correlation-Id": corr}),
    )
    async with db.connect() as conn:
        events = await fetch_all(conn, "SELECT * FROM domain_event_outbox")
    assert len(events) == 1
    ev = events[0]
    p = ev["payload"]
    assert ev["status"] == "PENDING" and ev["event_type"] == "WidgetCreated"
    assert p["event_id"] == str(ev["event_id"])
    assert p["event_version"] == 1
    assert p["correlation_id"] == corr
    assert p["causation_id"] == r.headers["X-Request-Id"]
    assert p["aggregate_id"] == r.json()["data"]["id"] == str(ev["aggregate_id"])
    assert p["data"] == {"name": "a"}


# --- student tenancy ---------------------------------------------------------------------------


async def test_student_context_resolves_through_active_enrollment(db):
    async with db.begin() as conn:
        school = await create_school(conn)
        structure = await create_year_grade_section(conn, school)
        user, student = await create_student(conn, school, structure)
    from app.auth import load_actor

    async with db.connect() as conn:
        ctx = await resolve_student_context(conn, await load_actor(conn, user))
    assert ctx.student_id == student and ctx.section_id == structure["section_id"]
    assert ctx.school_id == school


@pytest.mark.parametrize("year_status,enrollment_status", [("ACTIVE", "TRANSFERRED"), ("CLOSED", "ACTIVE")])
async def test_student_without_current_active_enrollment_is_rejected(db, year_status, enrollment_status):
    async with db.begin() as conn:
        school = await create_school(conn)
        structure = await create_year_grade_section(conn, school, year_status=year_status)
        user, _ = await create_student(conn, school, structure, enrollment_status=enrollment_status)
    from app.auth import load_actor

    async with db.connect() as conn:
        actor = await load_actor(conn, user)
        with pytest.raises(errors.AppError) as exc:
            await resolve_student_context(conn, actor)
    assert exc.value.code == "ENROLLMENT_INACTIVE"


async def test_migration_020_allows_transfer_history(db):
    """One ACTIVE enrollment per student-year, but TRANSFERRED history rows may coexist."""
    async with db.begin() as conn:
        school = await create_school(conn)
        structure = await create_year_grade_section(conn, school)
        _, student = await create_student(conn, school, structure)
        await execute(conn, "UPDATE student_enrollments SET status='TRANSFERRED' WHERE student_id=:s", s=student)
        await execute(
            conn,
            """INSERT INTO student_enrollments (school_id, student_id,
            academic_year_id, grade_id, section_id, enrollment_date)
            VALUES (:sc, :s, :y, :g, :sec, DATE '2026-10-01')""",
            sc=school,
            s=student,
            y=structure["academic_year_id"],
            g=structure["grade_id"],
            sec=structure["section_id"],
        )
    with pytest.raises(Exception, match="student_enrollments_active_uq"):
        async with db.begin() as conn:
            await execute(
                conn,
                """INSERT INTO student_enrollments (school_id, student_id,
                academic_year_id, grade_id, section_id, enrollment_date)
                VALUES (:sc, :s, :y, :g, :sec, DATE '2026-10-02')""",
                sc=school,
                s=student,
                y=structure["academic_year_id"],
                g=structure["grade_id"],
                sec=structure["section_id"],
            )
