"""Real logins (Supabase JWT + Auth Admin API), serverless database mode, CORS and worker triggers."""

import asyncio
import json
import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app import auth as auth_module
from app.ai import transcription
from app.config import DEV_JWT_SECRET, get_settings
from app.db import Database, fetch_one
from app.identity import LoginRequest, SupabaseIdentityProvider
from app.main import create_app
from tests.factories import auth
from tests.test_slice1 import _stubs, call, err, ok, ready_lesson, sarah, school, storage, uid  # noqa: F401


# ------------------------------------------------------------------------------------------------
# JWT verification
# ------------------------------------------------------------------------------------------------
@pytest.fixture
def es256(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    s = get_settings()
    monkeypatch.setattr(s, "supabase_url", "https://proj.supabase.co")

    class FakeJWKS:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": key.public_key()})()

    monkeypatch.setattr(auth_module, "_jwks", lambda: FakeJWKS())
    return key


def es_token(key, sub, *, iss="https://proj.supabase.co/auth/v1", **extra) -> str:
    now = int(time.time())
    claims = {"sub": str(sub), "aud": "authenticated", "iat": now, "exp": now + 600, "iss": iss, **extra}
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": "k1"})


async def test_signing_key_tokens_are_verified(client, school, es256):
    user = sarah(school)
    r = await client.get("/v1/me", headers={"Authorization": f"Bearer {es_token(es256, user)}"})
    me = ok(r)
    assert me["homes"] == ["TEACHER"] and me["schools"][0]["roles"] == ["TEACHER"]
    bad_iss = es_token(es256, user, iss="https://evil.example/auth/v1")
    err(await client.get("/v1/me", headers={"Authorization": f"Bearer {bad_iss}"}), 401, "UNAUTHENTICATED")
    other = ec.generate_private_key(ec.SECP256R1())
    err(
        await client.get("/v1/me", headers={"Authorization": f"Bearer {es_token(other, user)}"}), 401, "UNAUTHENTICATED"
    )


async def test_shared_secret_can_be_disabled(client, school, monkeypatch):
    headers = auth(uid(sarah(school)))  # minted with the dev secret
    monkeypatch.setattr(get_settings(), "jwt_secret", None)
    err(await client.get("/v1/me", headers=headers), 401, "UNAUTHENTICATED")


def test_production_never_accepts_the_dev_secret(monkeypatch):
    monkeypatch.setenv("AAYRA_ENVIRONMENT", "production")
    get_settings.cache_clear()
    try:
        assert get_settings().jwt_secret is None
    finally:
        monkeypatch.delenv("AAYRA_ENVIRONMENT")
        get_settings.cache_clear()
    assert get_settings().jwt_secret == DEV_JWT_SECRET


async def test_unknown_login_gets_no_account_code(client):
    err(await call(client, "GET", "/v1/me", uuid.uuid4()), 401, "NO_ACCOUNT")


async def test_me_lists_homes_for_each_role(client, school):
    arjun = school["students"][0]["user_id"]
    me = ok(await call(client, "GET", "/v1/me", arjun))
    assert me["homes"] == ["STUDENT"] and me["student"]["section_name"] == "10A"
    admin = ok(await call(client, "GET", "/v1/me", school["admin_user_id"]))
    assert admin["homes"] == ["ADMIN"]
    ok(await call(client, "POST", "/v1/independent-learners", sarah(school),
                  {"display_name": "Sarah", "date_of_birth": "1990-01-01", "confirms_18_or_older": True,
                   "country_code": "US", "timezone": "America/Phoenix"}), 201)  # fmt: skip
    assert ok(await call(client, "GET", "/v1/me", sarah(school)))["homes"] == ["INDEPENDENT", "TEACHER"]


# ------------------------------------------------------------------------------------------------
# Supabase Auth Admin API
# ------------------------------------------------------------------------------------------------
@pytest.fixture
def supabase_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(s, "supabase_service_role_key", "service-key")
    monkeypatch.setattr(s, "web_app_url", "https://aayra-web.vercel.app")
    return s


async def test_supabase_invites_and_username_logins(supabase_settings):
    seen = []
    uid1, uid2 = uuid.uuid4(), uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/auth/v1/invite":
            return httpx.Response(200, json={"id": str(uid1)})
        if request.url.path == "/auth/v1/admin/users" and request.method == "POST":
            body = json.loads(request.content)
            if body["email"].startswith("taken"):
                return httpx.Response(422, json={"msg": "A user with this email address has already been registered"})
            return httpx.Response(200, json={"id": str(uid2)})
        return httpx.Response(200, json={"id": str(uid2)})

    p = SupabaseIdentityProvider(transport=httpx.MockTransport(handler))
    invited = await p.provision_login(LoginRequest("Sarah", "sarah@school.org"))
    assert (invited.user_id, invited.temporary_password) == (uid1, None)
    inv = seen[0]
    assert inv.headers["apikey"] == "service-key" and inv.url.params["redirect_to"] == "https://aayra-web.vercel.app"
    student = await p.provision_login(LoginRequest("Arjun", None, "demohs.10a001"))
    body = json.loads(seen[1].content)
    assert student.user_id == uid2 and len(student.temporary_password) == 12
    assert body["email"] == "demohs.10a001@students.aayra.invalid" and body["email_confirm"] is True
    assert body["user_metadata"]["must_change_password"] is True and body["password"] == student.temporary_password
    with pytest.raises(Exception) as exc:
        await p.provision_login(LoginRequest("X", None, "taken.user"))
    assert exc.value.code == "LOGIN_ALREADY_EXISTS"
    reset = await p.reset_temporary_password(uid2)
    assert seen[-1].method == "PUT" and seen[-1].url.path == f"/auth/v1/admin/users/{uid2}" and len(reset) == 12
    demo = SupabaseIdentityProvider(transport=httpx.MockTransport(handler), password_logins=True)
    staff = await demo.provision_login(LoginRequest("Ravi", "ravi@school.org"))
    assert staff.temporary_password and json.loads(seen[-1].content)["email"] == "ravi@school.org"


async def test_student_creation_returns_one_time_password_and_reset(client, school):
    admin = school["admin_user_id"]
    created = ok(await call(client, "POST", "/v1/students", admin, {
        "section_id": school["section_id"], "display_name": "New Kid", "admission_id": "N-1",
        "date_of_birth": "2011-03-03"}), 201)  # fmt: skip
    assert "temporary_password" in created  # None with the stub provider
    reset = ok(await call(client, "POST", f"/v1/students/{created['student_id']}/reset-login", admin))
    assert len(reset["temporary_password"]) == 12
    err(await call(client, "POST", f"/v1/students/{created['student_id']}/reset-login", sarah(school)), 403,
        "INSUFFICIENT_ROLE_SCOPE")  # fmt: skip


# ------------------------------------------------------------------------------------------------
# Serverless database mode, CORS, worker triggers
# ------------------------------------------------------------------------------------------------
async def test_serverless_database_mode_works(db):
    url = db.engine.url.render_as_string(hide_password=False)
    serverless = Database(url, serverless=True)
    try:
        async with serverless.begin() as conn:
            for _ in range(3):  # repeated identical statements must not rely on a statement cache
                assert (await fetch_one(conn, "SELECT count(*) AS n FROM roles WHERE role_code <> :x", x="Z"))["n"] == 5
    finally:
        await serverless.dispose()
    assert serverless.engine.pool.__class__.__name__ == "NullPool"


async def test_cors_allows_the_web_app_only(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "cors_origins", "https://aayra-web.vercel.app")
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        pre = await c.options("/v1/me", headers={"Origin": "https://aayra-web.vercel.app",
                                                 "Access-Control-Request-Method": "GET"})  # fmt: skip
        assert pre.headers["access-control-allow-origin"] == "https://aayra-web.vercel.app"
        bad = await c.options(
            "/v1/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
        )
        assert "access-control-allow-origin" not in bad.headers


async def test_worker_kick_and_scheduled_run(client, db, storage, school, monkeypatch):
    from tests.test_slice1 import upload_pdf

    item = await upload_pdf(client, storage, sarah(school))
    lesson = ok(await call(client, "POST", "/v1/lessons", sarah(school), {"subject_id": school["subjects"]["BIO"],
                "academic_year_id": school["academic_year_id"], "title": "Kick"}), 201)  # fmt: skip
    ok(await call(client, "POST", f"/v1/lessons/{lesson['lesson_id']}/content", sarah(school),
                  {"content_item_id": item["content_item_id"], "display_order": 1}), 201)  # fmt: skip
    kicked = ok(await call(client, "POST", "/v1/worker/kick", sarah(school)))
    assert kicked["ran"] >= 2 and kicked["waiting"] == 0
    assert ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", sarah(school)))["status"] == "READY"
    err(await client.post("/internal/worker/run"), 401, "UNAUTHENTICATED")
    monkeypatch.setattr(get_settings(), "worker_secret", "w-secret")
    err(await client.post("/internal/worker/run", headers={"Authorization": "Bearer nope"}), 401, "UNAUTHENTICATED")
    assert ok(await client.post("/internal/worker/run", headers={"Authorization": "Bearer w-secret"}))["ran"] == 0
    monkeypatch.setenv("CRON_SECRET", "vercel-cron")
    assert ok(await client.get("/internal/worker/run", headers={"Authorization": "Bearer vercel-cron"}))["waiting"] == 0
    assert auth  # imported helper kept for parity with other test modules


async def test_bundled_ffmpeg_measures_duration(monkeypatch):
    import subprocess

    exe = transcription.ffmpeg_path()
    if exe is None:
        pytest.skip("no ffmpeg available")
    monkeypatch.setattr(transcription.shutil, "which", lambda name: None)  # no system ffmpeg/ffprobe
    path = "/tmp/aayra-tone-test.mp3"
    await asyncio.to_thread(
        subprocess.run, [exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2", path], check=True
    )
    assert transcription.ffmpeg_path() is not None  # falls back to the imageio-ffmpeg binary
    duration = await transcription.probe_duration(path, transcription.ffmpeg_path())
    assert 1.5 < duration < 2.5
