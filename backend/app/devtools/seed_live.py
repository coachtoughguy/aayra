"""Seed the demo school into a REAL deployment (your Supabase project) so you can sign in as every role.

Runs the API in-process against AAYRA_DATABASE_URL and creates real Supabase Auth logins. Every
account gets a temporary password (no emails are sent); the web app asks for a new password at
first sign-in. The logins are written to demo_logins.csv (keep it private, delete it after use).

Creates: the demo school (2026-27, Grade 10, 10A, Biology + Mathematics), admin, 3 teachers,
8 students (usernames, e.g. demohs.10a001), and a parent linked to the first student.

    cd backend
    AAYRA_DATABASE_URL=... AAYRA_SUPABASE_URL=... AAYRA_SUPABASE_SERVICE_ROLE_KEY=... \\
      uv run python -m app.devtools.seed_live --email-domain yourdomain.com

--email-domain: domain for the demo staff/parent logins (addresses never receive mail, but use a
domain you control). Students sign in with a username.
"""

import argparse
import asyncio
import csv
import secrets
import time
import uuid

import httpx
import jwt

from app.devtools.seed_demo import seed_demo
from app.identity import LoginRequest, ProvisionedLogin, SupabaseIdentityProvider, set_identity_provider


class RecordingProvider(SupabaseIdentityProvider):
    def __init__(self) -> None:
        super().__init__(password_logins=True)
        self.created: list[tuple[LoginRequest, ProvisionedLogin]] = []

    async def provision_login(self, request: LoginRequest) -> ProvisionedLogin:
        login = await super().provision_login(request)
        self.created.append((request, login))
        return login


def _write_csv(path: str, rows: list[list]) -> None:
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


async def main(email_domain: str, students: int, out: str) -> None:  # pragma: no cover - CLI
    from app.config import get_settings
    from app.devtools import seed_demo as demo
    from app.main import create_app

    s = get_settings()
    if not (s.supabase_url and s.supabase_service_role_key):
        raise SystemExit("Set AAYRA_SUPABASE_URL and AAYRA_SUPABASE_SERVICE_ROLE_KEY (and AAYRA_DATABASE_URL).")
    # In-process only: a throwaway signing secret and bootstrap key for this run's own requests.
    s.jwt_secret = secrets.token_urlsafe(48)
    s.bootstrap_key = secrets.token_urlsafe(24)
    s.identity_provider = "supabase"
    provider = RecordingProvider()
    set_identity_provider(provider)
    demo.STUDENT_NAMES[:] = demo.STUDENT_NAMES[:students]

    def token_for(user_id: uuid.UUID) -> str:
        now = int(time.time())
        return jwt.encode({"sub": str(user_id), "aud": s.jwt_audience, "iat": now, "exp": now + 3600},
                          s.jwt_secret, algorithm="HS256")  # fmt: skip

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://seed") as client:
        result = await seed_demo(client, bootstrap_key=s.bootstrap_key, token_for=token_for, email_domain=email_domain)
        first = result["students"][0]
        r = await client.post(
            f"/v1/students/{first['student_id']}/guardians",
            json={"email": f"parent@{email_domain}", "display_name": "Neha Mehta", "relationship_type": "MOTHER"},
            headers={"Authorization": f"Bearer {token_for(uuid.UUID(result['admin_user_id']))}",
                     "Idempotency-Key": f"seed-parent-{first['student_id']}"},
        )  # fmt: skip
        r.raise_for_status()
    rows = [["name", "login", "temporary_password"]] + [
        [req.display_name, login.login, login.temporary_password] for req, login in provider.created
    ]
    await asyncio.to_thread(_write_csv, out, rows)
    print(f"Seeded {result['school_code']}: {len(provider.created)} logins written to {out}")


if __name__ == "__main__":  # pragma: no cover
    p = argparse.ArgumentParser()
    p.add_argument("--email-domain", required=True)
    p.add_argument("--students", type=int, default=8)
    p.add_argument("--out", default="demo_logins.csv")
    a = p.parse_args()
    asyncio.run(main(a.email_domain, a.students, a.out))
