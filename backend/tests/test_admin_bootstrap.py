"""Contract §5 — minimal Admin bootstrap path."""

import uuid

import pytest

from app.auth import load_actor, resolve_student_context
from app.config import get_settings
from app.db import fetch_all, fetch_one
from app.devtools.seed_demo import STUDENT_NAMES, seed_demo
from app.identity import StubIdentityProvider, set_identity_provider
from tests.factories import auth, token_for

KEY = "test-bootstrap-key"


@pytest.fixture(autouse=True)
def _bootstrap_settings():
    settings = get_settings()
    previous = settings.bootstrap_key
    settings.bootstrap_key = KEY
    set_identity_provider(StubIdentityProvider())
    yield
    settings.bootstrap_key = previous
    set_identity_provider(None)


def school_body(code: str = "TEST-HS", admin_email: str = "admin@school.example.com") -> dict:
    return {
        "name": "Test High",
        "school_code": code,
        "market_region": "US",
        "timezone": "America/Phoenix",
        "country_code": "us",
        "admin": {"email": admin_email, "display_name": "Alex Admin"},
    }


async def bootstrap(client, **kw) -> dict:
    r = await client.post("/v1/schools", json=school_body(**kw), headers={"X-Bootstrap-Key": KEY})
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def post(client, path: str, user, body: dict, key: str | None = None):
    headers = auth(uuid.UUID(str(user)), **{"Idempotency-Key": key or uuid.uuid4().hex})
    return await client.post(path, json=body, headers=headers)


async def structure(client, school: dict) -> dict:
    admin = school["admin_user_id"]
    sid = school["school_id"]
    year = (
        await post(
            client,
            f"/v1/schools/{sid}/academic-years",
            admin,
            {"name": "2026-27", "start_date": "2026-06-01", "end_date": "2027-05-31"},
        )
    ).json()["data"]
    grade = (
        await post(
            client, f"/v1/academic-years/{year['academic_year_id']}/grades", admin, {"name": "G10", "grade_level": 10}
        )
    ).json()["data"]
    section = (await post(client, f"/v1/grades/{grade['grade_id']}/sections", admin, {"name": "10A"})).json()["data"]
    bio = (
        await post(
            client,
            f"/v1/academic-years/{year['academic_year_id']}/subjects",
            admin,
            {"code": "BIO", "name": "Biology", "grade_ids": [grade["grade_id"]]},
        )
    ).json()["data"]
    return {"year": year, "grade": grade, "section": section, "bio": bio}


async def invite(client, school: dict, name: str, email: str) -> dict:
    r = await post(
        client,
        "/v1/teachers",
        school["admin_user_id"],
        {"school_id": school["school_id"], "email": email, "display_name": name},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


# --- POST /v1/schools ---------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"X-Bootstrap-Key": "wrong"}])
async def test_bootstrap_requires_platform_key(client, headers):
    r = await client.post("/v1/schools", json=school_body(), headers=headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "BOOTSTRAP_FORBIDDEN"


async def test_bootstrap_disabled_when_no_key_configured(client):
    get_settings().bootstrap_key = None
    r = await client.post("/v1/schools", json=school_body(), headers={"X-Bootstrap-Key": KEY})
    assert r.status_code == 403


async def test_bootstrap_creates_school_admin_audit_and_event(client, db):
    data = await bootstrap(client)
    async with db.connect() as conn:
        school = await fetch_one(conn, "SELECT * FROM schools WHERE school_id = :s", s=uuid.UUID(data["school_id"]))
        admin = await fetch_one(conn, "SELECT * FROM users WHERE user_id = :u", u=uuid.UUID(data["admin_user_id"]))
        roles = await fetch_all(
            conn,
            "SELECT r.role_code FROM school_user_roles sur JOIN roles r USING (role_id) WHERE user_id = :u",
            u=admin["user_id"],
        )
        audit_rows = await fetch_all(conn, "SELECT action FROM audit_events")
        events = await fetch_all(conn, "SELECT event_type FROM domain_event_outbox")
    assert school["status"] == "SETUP" and school["country_code"] == "US"
    assert admin["account_status"] == "INVITED"
    assert [r["role_code"] for r in roles] == ["SCHOOL_ADMIN"]
    assert [a["action"] for a in audit_rows] == ["SCHOOL_BOOTSTRAPPED"]
    assert [e["event_type"] for e in events] == ["SchoolCreated"]


async def test_duplicate_school_code_is_conflict(client):
    await bootstrap(client)
    r = await client.post(
        "/v1/schools", json=school_body(admin_email="b@x.example.com"), headers={"X-Bootstrap-Key": KEY}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "SCHOOL_CODE_TAKEN"


async def test_first_authenticated_request_activates_invited_admin(client, db):
    school = await bootstrap(client)
    r = await client.get("/v1/me", headers=auth(uuid.UUID(school["admin_user_id"])))
    assert r.status_code == 200
    assert r.json()["data"]["roles"] == {school["school_id"]: ["SCHOOL_ADMIN"]}
    async with db.connect() as conn:
        u = await fetch_one(
            conn, "SELECT account_status FROM users WHERE user_id = :u", u=uuid.UUID(school["admin_user_id"])
        )
        a = await fetch_one(
            conn, "SELECT state FROM account_activation_state WHERE user_id = :u", u=uuid.UUID(school["admin_user_id"])
        )
    assert u["account_status"] == "ACTIVE" and a["state"] == "activated"


# --- academic structure -------------------------------------------------------------------------


async def test_school_activates_once_structure_exists(client, db):
    school = await bootstrap(client)
    sid = uuid.UUID(school["school_id"])

    async def status() -> str:
        async with db.connect() as conn:
            return (await fetch_one(conn, "SELECT status FROM schools WHERE school_id = :s", s=sid))["status"]

    admin = school["admin_user_id"]
    year = (
        await post(
            client,
            f"/v1/schools/{sid}/academic-years",
            admin,
            {"name": "Y", "start_date": "2026-06-01", "end_date": "2027-05-31"},
        )
    ).json()["data"]
    grade = (
        await post(
            client, f"/v1/academic-years/{year['academic_year_id']}/grades", admin, {"name": "G", "grade_level": 9}
        )
    ).json()["data"]
    await post(client, f"/v1/grades/{grade['grade_id']}/sections", admin, {"name": "9A"})
    assert await status() == "SETUP"  # no subject yet
    await post(
        client,
        f"/v1/academic-years/{year['academic_year_id']}/subjects",
        admin,
        {"code": "M", "name": "Math", "grade_ids": [grade["grade_id"]]},
    )
    assert await status() == "ACTIVE"


async def test_only_one_active_academic_year(client):
    school = await bootstrap(client)
    await structure(client, school)
    r = await post(
        client,
        f"/v1/schools/{school['school_id']}/academic-years",
        school["admin_user_id"],
        {"name": "2027-28", "start_date": "2027-06-01", "end_date": "2028-05-31"},
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "ACADEMIC_YEAR_ALREADY_ACTIVE"
    planned = await post(
        client,
        f"/v1/schools/{school['school_id']}/academic-years",
        school["admin_user_id"],
        {"name": "2027-28", "start_date": "2027-06-01", "end_date": "2028-05-31", "status": "PLANNED"},
    )
    assert planned.status_code == 201


async def test_duplicate_section_and_subject_codes_are_conflicts(client):
    school = await bootstrap(client)
    s = await structure(client, school)
    admin = school["admin_user_id"]
    r1 = await post(client, f"/v1/grades/{s['grade']['grade_id']}/sections", admin, {"name": "10a"})
    r2 = await post(
        client,
        f"/v1/academic-years/{s['year']['academic_year_id']}/subjects",
        admin,
        {"code": "BIO", "name": "Bio again", "grade_ids": [s["grade"]["grade_id"]]},
    )
    assert r1.json()["error"]["code"] == "SECTION_NAME_TAKEN"
    assert r2.json()["error"]["code"] == "SUBJECT_CODE_TAKEN"


async def test_subject_grades_must_belong_to_the_year(client):
    school = await bootstrap(client)
    s = await structure(client, school)
    r = await post(
        client,
        f"/v1/academic-years/{s['year']['academic_year_id']}/subjects",
        school["admin_user_id"],
        {"code": "X", "name": "X", "grade_ids": [str(uuid.uuid4())]},
    )
    assert r.status_code == 422


# --- authorization ------------------------------------------------------------------------------


async def test_non_admins_and_other_schools_admins_are_forbidden(client):
    a = await bootstrap(client, code="A-HS", admin_email="a@a.example.com")
    b = await bootstrap(client, code="B-HS", admin_email="b@b.example.com")
    s = await structure(client, a)
    teacher = await invite(client, a, "Sarah", "sarah@a.example.com")
    path = f"/v1/academic-years/{s['year']['academic_year_id']}/grades"
    for user in (teacher["user_id"], b["admin_user_id"]):
        r = await post(client, path, user, {"name": "G11", "grade_level": 11})
        assert r.status_code == 403 and r.json()["error"]["code"] == "INSUFFICIENT_ROLE_SCOPE"


async def test_unknown_parent_is_404(client):
    school = await bootstrap(client)
    r = await post(
        client, f"/v1/academic-years/{uuid.uuid4()}/grades", school["admin_user_id"], {"name": "G", "grade_level": 1}
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


# --- teachers & mappings ------------------------------------------------------------------------


async def test_invite_teacher_creates_profile_role_and_invite(client, db):
    school = await bootstrap(client)
    t = await invite(client, school, "Sarah", "sarah@school.example.com")
    async with db.connect() as conn:
        inv = await fetch_one(
            conn, "SELECT state, invited_by FROM account_activation_state WHERE user_id = :u", u=uuid.UUID(t["user_id"])
        )
    assert inv["state"] == "invite_sent" and str(inv["invited_by"]) == school["admin_user_id"]
    again = await post(
        client,
        "/v1/teachers",
        school["admin_user_id"],
        {"school_id": school["school_id"], "email": "SARAH@school.example.com", "display_name": "Sarah"},
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "TEACHER_ALREADY_EXISTS"


async def test_one_person_can_be_admin_and_teacher(client):
    school = await bootstrap(client)
    t = await invite(client, school, "Alex Admin", "admin@school.example.com")
    assert t["user_id"] == school["admin_user_id"]
    r = await client.get("/v1/me", headers=auth(uuid.UUID(t["user_id"])))
    assert r.json()["data"]["roles"][school["school_id"]] == ["SCHOOL_ADMIN", "TEACHER"]


async def test_mapping_rules_are_enforced(client):
    school = await bootstrap(client)
    s = await structure(client, school)
    admin = school["admin_user_id"]
    sarah = await invite(client, school, "Sarah", "sarah@school.example.com")
    ravi = await invite(client, school, "Ravi", "ravi@school.example.com")
    priya = await invite(client, school, "Priya", "priya@school.example.com")
    sec, bio = s["section"]["section_id"], s["bio"]["subject_id"]

    async def assign(t, role, subject=None):
        body = {"teacher_id": t["teacher_id"], "section_id": sec, "assignment_role": role}
        if subject:
            body["subject_id"] = subject
        return await post(client, "/v1/teacher-assignments", admin, body)

    assert (await assign(sarah, "CLASS_TEACHER")).status_code == 201
    assert (await assign(sarah, "PRIMARY_SUBJECT_TEACHER", bio)).status_code == 201
    r = await assign(ravi, "CLASS_TEACHER")
    assert r.status_code == 409 and r.json()["error"]["code"] == "CLASS_TEACHER_ALREADY_ASSIGNED"
    r = await assign(ravi, "PRIMARY_SUBJECT_TEACHER", bio)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PRIMARY_TEACHER_ALREADY_ASSIGNED"
    assert (await assign(ravi, "CO_TEACHER", bio)).status_code == 201
    assert (await assign(priya, "CO_TEACHER", bio)).status_code == 201  # many co-teachers allowed
    assert (await assign(priya, "CLASS_TEACHER", bio)).status_code == 422  # class teacher takes no subject
    assert (await assign(priya, "PRIMARY_SUBJECT_TEACHER")).status_code == 422  # subject required


async def test_subject_must_be_offered_in_the_sections_grade(client):
    school = await bootstrap(client)
    s = await structure(client, school)
    admin = school["admin_user_id"]
    other_grade = (
        await post(
            client,
            f"/v1/academic-years/{s['year']['academic_year_id']}/grades",
            admin,
            {"name": "G11", "grade_level": 11},
        )
    ).json()["data"]
    chem = (
        await post(
            client,
            f"/v1/academic-years/{s['year']['academic_year_id']}/subjects",
            admin,
            {"code": "CHEM", "name": "Chemistry", "grade_ids": [other_grade["grade_id"]]},
        )
    ).json()["data"]
    t = await invite(client, school, "Sarah", "sarah@school.example.com")
    r = await post(
        client,
        "/v1/teacher-assignments",
        admin,
        {
            "teacher_id": t["teacher_id"],
            "section_id": s["section"]["section_id"],
            "subject_id": chem["subject_id"],
            "assignment_role": "PRIMARY_SUBJECT_TEACHER",
        },
    )
    assert r.status_code == 422


# --- students -----------------------------------------------------------------------------------


def student_body(section_id: str, **kw) -> dict:
    return {
        "section_id": section_id,
        "display_name": "Arjun Mehta",
        "admission_id": "10A-001",
        "date_of_birth": "2011-03-14",
        "enrollment_date": "2026-06-01",
        **kw,
    }


async def test_create_student_enrolls_and_emits_activation_event(client, db):
    school = await bootstrap(client)
    s = await structure(client, school)
    r = await post(client, "/v1/students", school["admin_user_id"], student_body(s["section"]["section_id"]))
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["username"] == "tesths.10a001"
    assert data["guardian_status"] == "PRIMARY_GUARDIAN_PENDING"
    async with db.connect() as conn:
        actor = await load_actor(conn, uuid.UUID(data["user_id"]))
        ctx = await resolve_student_context(conn, actor)
        events = await fetch_all(
            conn,
            "SELECT event_type, payload FROM domain_event_outbox WHERE event_type LIKE 'Student%' ORDER BY event_type",
        )
    assert str(ctx.student_id) == data["student_id"] and str(ctx.section_id) == s["section"]["section_id"]
    assert [e["event_type"] for e in events] == ["StudentCreated", "StudentEnrollmentActivated"]
    activated = events[1]["payload"]
    assert activated["data"]["section_id"] == s["section"]["section_id"]
    assert activated["data"]["effective_at"] == "2026-06-01"
    assert activated["causation_id"] == events[0]["payload"]["event_id"]


async def test_student_under_13_is_rejected(client):
    school = await bootstrap(client)
    s = await structure(client, school)
    r = await post(
        client,
        "/v1/students",
        school["admin_user_id"],
        student_body(s["section"]["section_id"], date_of_birth="2013-06-02"),  # 12 on 2026-06-01
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "AGE_BELOW_MINIMUM"


async def test_duplicate_admission_id_is_conflict(client):
    school = await bootstrap(client)
    s = await structure(client, school)
    await post(client, "/v1/students", school["admin_user_id"], student_body(s["section"]["section_id"]))
    r = await post(
        client, "/v1/students", school["admin_user_id"], student_body(s["section"]["section_id"], display_name="Other")
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "ADMISSION_ID_TAKEN"


async def test_student_create_retry_with_same_key_is_replayed(client, db):
    school = await bootstrap(client)
    s = await structure(client, school)
    body = student_body(s["section"]["section_id"])
    first = await post(client, "/v1/students", school["admin_user_id"], body, key="enroll-arjun")
    second = await post(client, "/v1/students", school["admin_user_id"], body, key="enroll-arjun")
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert first.json()["data"] == second.json()["data"]
    async with db.connect() as conn:
        n = await fetch_one(conn, "SELECT count(*) AS n FROM student_enrollments")
    assert n["n"] == 1


# --- seed ----------------------------------------------------------------------------------------


async def test_seed_demo_builds_the_canonical_school(client, db):
    result = await seed_demo(client, bootstrap_key=KEY, token_for=token_for)
    assert len(result["students"]) == 28 and result["students"][0]["name"] == "Arjun Mehta"
    sec = uuid.UUID(result["section_id"])
    async with db.connect() as conn:
        school = await fetch_one(
            conn, "SELECT status FROM schools WHERE school_id = :s", s=uuid.UUID(result["school_id"])
        )
        roster = await fetch_one(
            conn, "SELECT count(*) AS n FROM student_enrollments WHERE section_id = :s AND status = 'ACTIVE'", s=sec
        )
        mappings = await fetch_all(
            conn,
            """SELECT u.display_name, a.assignment_role, sub.code
                 FROM teacher_section_subject_assignments a
                 JOIN teacher_profiles t USING (teacher_id) JOIN users u ON u.user_id = t.user_id
                 LEFT JOIN subjects sub ON sub.subject_id = a.subject_id
                WHERE a.section_id = :s AND a.status = 'ACTIVE'
                ORDER BY u.display_name, a.assignment_role""",
            s=sec,
        )
        activations = await fetch_one(
            conn, "SELECT count(*) AS n FROM domain_event_outbox WHERE event_type = 'StudentEnrollmentActivated'"
        )
    assert school["status"] == "ACTIVE"
    assert roster["n"] == len(STUDENT_NAMES) == 28
    assert activations["n"] == 28
    assert [(m["display_name"], m["assignment_role"], m["code"]) for m in mappings] == [
        ("Priya Nair", "CO_TEACHER", "BIO"),
        ("Ravi Kumar", "PRIMARY_SUBJECT_TEACHER", "MATH"),
        ("Sarah Johnson", "CLASS_TEACHER", None),
        ("Sarah Johnson", "PRIMARY_SUBJECT_TEACHER", "BIO"),
    ]
