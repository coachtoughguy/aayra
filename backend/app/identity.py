"""Login provisioning.

`users.user_id` MUST equal the Supabase Auth user id (`auth.uid()`), because every RLS helper
compares the two. So creating an Aayra user starts by creating (or inviting) the login in
Supabase Auth and using the id it returns.

- `StubIdentityProvider` (local, tests, seed script): returns a fresh UUID; tests then mint
  JWTs for that id with the local secret.
- `SupabaseIdentityProvider` (AAYRA_IDENTITY_PROVIDER=supabase): Supabase Auth Admin API with the
  service-role key (server side only).
    * People with an email get an invite email; the link opens the web app, where they set a password.
    * Students without an email (doc 21 v1.0) get a username login. Supabase Auth needs an email,
      so the username maps to `<username>@<AAYRA_STUDENT_LOGIN_DOMAIN>` and the account is created
      with a one-time temporary password that the admin hands over. `must_change_password` in the
      user's metadata makes the web app ask for a new password at first login (doc 21 Sept 9:
      temporary/activation credentials and first-login password setup).
"""

import secrets
import string
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

import httpx

from app import errors
from app.config import get_settings


@dataclass(frozen=True)
class LoginRequest:
    display_name: str
    email: str | None  # students may have no email (doc 21 v1.0); they get a username login
    username: str | None = None


@dataclass(frozen=True)
class ProvisionedLogin:
    user_id: UUID
    # Only for username logins: shown once to the admin, never stored by Aayra.
    temporary_password: str | None = None
    login: str | None = None  # the email or username the person signs in with


class IdentityProvider(Protocol):
    async def provision_login(self, request: LoginRequest) -> ProvisionedLogin: ...
    async def reset_temporary_password(self, user_id: UUID) -> str: ...


def temporary_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


class StubIdentityProvider:
    def __init__(self) -> None:
        self.provisioned: list[tuple[UUID, LoginRequest]] = []

    async def provision_login(self, request: LoginRequest) -> ProvisionedLogin:
        user_id = uuid4()
        self.provisioned.append((user_id, request))
        return ProvisionedLogin(user_id, None, request.email or request.username)

    async def reset_temporary_password(self, user_id: UUID) -> str:
        return temporary_password()


class SupabaseIdentityProvider:
    """`password_logins=True` (demo/test seeding only): emailed accounts also get a temporary password
    instead of an invite email, so a tester can sign in as every role without real inboxes."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, *, password_logins: bool = False):
        self.password_logins = password_logins
        s = get_settings()
        if not (s.supabase_url and s.supabase_service_role_key):
            raise RuntimeError(
                "AAYRA_SUPABASE_URL and AAYRA_SUPABASE_SERVICE_ROLE_KEY are required for Supabase logins"
            )
        self.domain = s.student_login_domain
        self.redirect = s.web_app_url
        self.client = httpx.AsyncClient(
            base_url=s.supabase_url.rstrip("/") + "/auth/v1",
            timeout=20,
            transport=transport,
            headers={"apikey": s.supabase_service_role_key, "Authorization": f"Bearer {s.supabase_service_role_key}"},
        )

    async def _check(self, r: httpx.Response) -> dict:
        if r.status_code in (409, 422) and "already" in r.text.lower():
            raise errors.AppError("LOGIN_ALREADY_EXISTS", 409, "A login with this email or username already exists.")
        if r.status_code >= 400:
            raise errors.AppError("IDENTITY_PROVIDER_ERROR", 502, f"Could not create the login (HTTP {r.status_code}).")
        return r.json()

    async def provision_login(self, request: LoginRequest) -> ProvisionedLogin:
        meta = {"display_name": request.display_name}
        if request.email and not self.password_logins:
            params = {"redirect_to": self.redirect} if self.redirect else None
            body = await self._check(
                await self.client.post("/invite", params=params, json={"email": request.email, "data": meta})
            )
            return ProvisionedLogin(UUID(body["id"]), None, request.email)
        if not (request.username or request.email):
            raise errors.validation_error("A login needs an email or a username.")
        password = temporary_password()
        body = await self._check(
            await self.client.post(
                "/admin/users",
                json={
                    "email": request.email or f"{request.username}@{self.domain}",
                    "password": password,
                    "email_confirm": True,
                    "user_metadata": {**meta, "username": request.username, "must_change_password": True},
                },
            )
        )
        return ProvisionedLogin(UUID(body["id"]), password, request.email or request.username)

    async def reset_temporary_password(self, user_id: UUID) -> str:
        password = temporary_password()
        await self._check(
            await self.client.put(
                f"/admin/users/{user_id}", json={"password": password, "user_metadata": {"must_change_password": True}}
            )
        )
        return password


_provider: IdentityProvider | None = None


def get_identity_provider() -> IdentityProvider:
    global _provider
    if _provider is None:
        kind = get_settings().identity_provider
        _provider = SupabaseIdentityProvider() if kind == "supabase" else StubIdentityProvider()
    return _provider


def set_identity_provider(provider: IdentityProvider | None) -> None:
    global _provider
    _provider = provider
