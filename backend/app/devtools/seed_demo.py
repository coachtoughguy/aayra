"""Seed the canonical demo school THROUGH THE API (contract §5), so the seed exercises the same
code paths real admins will:

    Aayra Demo High School  ->  2026-27 (ACTIVE)  ->  Grade 10  ->  Section 10A
    Subjects: Biology (BIO), Mathematics (MATH)
    Sarah Johnson  - Class Teacher of 10A + Primary Biology teacher
    Ravi Kumar     - Primary Mathematics teacher
    Priya Nair     - Biology Co-teacher
    28 students in 10A (Arjun Mehta first)

Local only: it mints login tokens with the local JWT secret and relies on the stub identity
provider. Usage (from backend/):  uv run python -m app.devtools.seed_demo
"""

import asyncio
import hashlib
import json
import uuid
from datetime import date
from typing import Any

import httpx

STUDENT_NAMES = [
    "Arjun Mehta", "Aisha Khan", "Ben Carter", "Chloe Park", "Daniel Ortiz", "Diya Sharma",
    "Ethan Brooks", "Fatima Ali", "Gabriel Silva", "Hannah Lee", "Isaac Cohen", "Jaya Reddy",
    "Kai Nakamura", "Lena Fischer", "Marcus Green", "Maya Patel", "Noah Wilson", "Olivia Chen",
    "Omar Haddad", "Priya Iyer", "Quinn Murphy", "Rohan Gupta", "Sofia Rossi", "Tariq Hassan",
    "Uma Krishnan", "Victor Alvarez", "Wen Li", "Zara Ahmed",
]  # fmt: skip


class SeedError(RuntimeError):
    pass


async def seed_demo(
    client: httpx.AsyncClient,
    *,
    bootstrap_key: str,
    token_for,
    school_code: str = "DEMO-HS",
    email_domain: str = "demo.aayra.dev",
) -> dict[str, Any]:
    """Create the demo school via the public API. `token_for(user_id) -> str` mints a login token."""

    async def call(method: str, path: str, *, user: str | None = None, json_body: dict | None = None,
                   headers: dict | None = None) -> dict:  # fmt: skip
        h = dict(headers or {})
        if user:
            h["Authorization"] = f"Bearer {token_for(uuid.UUID(user))}"
        if method == "POST" and user:
            digest = hashlib.sha256(f"{path}|{json.dumps(json_body, sort_keys=True, default=str)}".encode())
            h["Idempotency-Key"] = f"seed-{digest.hexdigest()[:40]}"
        r = await client.request(method, path, json=json_body, headers=h)
        if r.status_code >= 400:
            raise SeedError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r.json()["data"]

    school = await call(
        "POST",
        "/v1/schools",
        headers={"X-Bootstrap-Key": bootstrap_key},
        json_body={
            "name": "Aayra Demo High School",
            "school_code": school_code,
            "market_region": "US",
            "timezone": "America/Phoenix",
            "country_code": "US",
            "admin": {"email": f"admin@{email_domain}", "display_name": "Alex Admin"},
        },
    )
    admin = school["admin_user_id"]
    sid = school["school_id"]

    year = await call(
        "POST",
        f"/v1/schools/{sid}/academic-years",
        user=admin,
        json_body={"name": "2026-27", "start_date": "2026-06-01", "end_date": "2027-05-31", "status": "ACTIVE"},
    )
    yid = year["academic_year_id"]
    grade = await call(
        "POST", f"/v1/academic-years/{yid}/grades", user=admin, json_body={"name": "Grade 10", "grade_level": 10}
    )
    section = await call("POST", f"/v1/grades/{grade['grade_id']}/sections", user=admin, json_body={"name": "10A"})
    sec = section["section_id"]
    bio = await call(
        "POST",
        f"/v1/academic-years/{yid}/subjects",
        user=admin,
        json_body={"code": "BIO", "name": "Biology", "grade_ids": [grade["grade_id"]]},
    )
    math = await call(
        "POST",
        f"/v1/academic-years/{yid}/subjects",
        user=admin,
        json_body={"code": "MATH", "name": "Mathematics", "grade_ids": [grade["grade_id"]]},
    )

    teachers: dict[str, dict] = {}
    for key, name in (("sarah", "Sarah Johnson"), ("ravi", "Ravi Kumar"), ("priya", "Priya Nair")):
        teachers[key] = await call(
            "POST",
            "/v1/teachers",
            user=admin,
            json_body={"school_id": sid, "email": f"{key}@{email_domain}", "display_name": name},
        )

    assignments = [
        ("sarah", None, "CLASS_TEACHER"),
        ("sarah", bio["subject_id"], "PRIMARY_SUBJECT_TEACHER"),
        ("ravi", math["subject_id"], "PRIMARY_SUBJECT_TEACHER"),
        ("priya", bio["subject_id"], "CO_TEACHER"),
    ]
    for key, subject_id, role in assignments:
        body = {"teacher_id": teachers[key]["teacher_id"], "section_id": sec, "assignment_role": role}
        if subject_id:
            body["subject_id"] = subject_id
        await call("POST", "/v1/teacher-assignments", user=admin, json_body=body)

    students = []
    for i, name in enumerate(STUDENT_NAMES, start=1):
        students.append(
            await call(
                "POST",
                "/v1/students",
                user=admin,
                json_body={
                    "section_id": sec,
                    "display_name": name,
                    "admission_id": f"10A-{i:03d}",
                    "date_of_birth": date(2011, (i % 12) + 1, (i % 27) + 1).isoformat(),
                    "enrollment_date": "2026-06-01",
                },
            )
        )

    return {
        "school_id": sid,
        "school_code": school_code,
        "admin_user_id": admin,
        "academic_year_id": yid,
        "grade_id": grade["grade_id"],
        "section_id": sec,
        "subjects": {"BIO": bio["subject_id"], "MATH": math["subject_id"]},
        "teachers": {k: {"teacher_id": v["teacher_id"], "user_id": v["user_id"]} for k, v in teachers.items()},
        "students": [
            {"student_id": s["student_id"], "user_id": s["user_id"], "name": s["display_name"]} for s in students
        ],  # fmt: skip
    }


async def _main() -> None:  # pragma: no cover - CLI
    import os

    from app.config import get_settings
    from app.main import create_app

    settings = get_settings()
    if settings.environment != "local" or settings.identity_provider != "stub":
        raise SystemExit("seed_demo only runs with AAYRA_ENVIRONMENT=local and the stub identity provider")
    key = settings.bootstrap_key or os.environ.setdefault("AAYRA_BOOTSTRAP_KEY", "local-bootstrap-key")
    settings.bootstrap_key = key

    import time

    import jwt

    def token_for(user_id: uuid.UUID) -> str:
        now = int(time.time())
        claims = {"sub": str(user_id), "aud": settings.jwt_audience, "role": "authenticated",
                  "iat": now, "exp": now + 3600}  # fmt: skip
        return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://seed") as client:
        result = await seed_demo(client, bootstrap_key=key, token_for=token_for)
    print(json.dumps({**result, "students": f"{len(result['students'])} students"}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
