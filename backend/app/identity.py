"""Login provisioning.

`users.user_id` MUST equal the Supabase Auth user id (`auth.uid()`), because every RLS helper
compares the two. So creating an Aayra user starts by creating (or inviting) the login in
Supabase Auth and using the id it returns.

- `StubIdentityProvider` (local, tests, seed script): returns a fresh UUID; tests then mint
  JWTs for that id with the local secret.
- `SupabaseIdentityProvider`: to be wired when real logins are connected (needs the project's
  service-role key; uses the Auth Admin API invite endpoint).
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from app.config import get_settings


@dataclass(frozen=True)
class LoginRequest:
    display_name: str
    email: str | None  # students may have no email (doc 21 v1.0); they get a username login
    username: str | None = None


class IdentityProvider(Protocol):
    async def provision_login(self, request: LoginRequest) -> UUID: ...


class StubIdentityProvider:
    def __init__(self) -> None:
        self.provisioned: list[tuple[UUID, LoginRequest]] = []

    async def provision_login(self, request: LoginRequest) -> UUID:
        user_id = uuid4()
        self.provisioned.append((user_id, request))
        return user_id


class SupabaseIdentityProvider:  # pragma: no cover - wired with real logins
    async def provision_login(self, request: LoginRequest) -> UUID:
        raise NotImplementedError("Supabase Auth provisioning is not wired yet; set AAYRA_IDENTITY_PROVIDER=stub.")


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
