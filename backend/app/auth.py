"""Authentication and role/tenancy resolution (contract §1 Auth, §3).

Supabase Auth issues the JWT; its `sub` IS `users.user_id` (the RLS helpers rely on
`auth.uid()` matching `users.user_id`). The backend verifies the token, loads the user and
their ACTIVE school roles, and endpoints then demand a role in a specific school.
"""

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

import jwt
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection

from app import errors
from app.config import get_settings
from app.db import execute, fetch_all, fetch_one, get_db

RoleCode = Literal["STUDENT", "TEACHER", "PARENT_GUARDIAN", "SCHOOL_ADMIN", "PRINCIPAL"]


@dataclass(frozen=True)
class Actor:
    user_id: UUID
    display_name: str
    # school_id -> {role_code, ...} for ACTIVE school_user_roles only
    roles: dict[UUID, frozenset[str]] = field(default_factory=dict)

    def has_role(self, school_id: UUID, *role_codes: str) -> bool:
        return bool(self.roles.get(school_id, frozenset()) & set(role_codes))

    def require_role(self, school_id: UUID, *role_codes: str) -> None:
        if not self.has_role(school_id, *role_codes):
            raise errors.insufficient_role_scope(required=list(role_codes))

    def schools_with_role(self, *role_codes: str) -> list[UUID]:
        return [s for s, r in self.roles.items() if r & set(role_codes)]


_jwks_client: "jwt.PyJWKClient | None" = None


def _jwks() -> "jwt.PyJWKClient":
    global _jwks_client
    s = get_settings()
    if not s.supabase_url:
        raise errors.unauthenticated("This token type needs AAYRA_SUPABASE_URL to be configured.")
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(
            f"{s.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json", cache_keys=True, lifespan=3600
        )
    return _jwks_client


def decode_token(token: str) -> dict:
    """Verify a Supabase access token: HS256 with the project's JWT secret, or ES256/RS256 with the
    project's published signing keys (JWKS)."""
    s = get_settings()
    try:
        alg = jwt.get_unverified_header(token).get("alg", "")
        if alg not in s.jwt_algorithms:
            raise errors.unauthenticated("Unsupported token algorithm.")
        if alg.startswith("HS"):
            if not s.jwt_secret:
                raise errors.unauthenticated("Shared-secret tokens are disabled.")
            key = s.jwt_secret
        else:
            key = _jwks().get_signing_key_from_jwt(token).key
        options = {"require": ["sub", "exp"]}
        issuer = f"{s.supabase_url.rstrip('/')}/auth/v1" if s.supabase_url and not alg.startswith("HS") else None
        return jwt.decode(token, key, algorithms=[alg], audience=s.jwt_audience, issuer=issuer, options=options)
    except jwt.PyJWTError as exc:
        raise errors.unauthenticated(f"Invalid access token: {exc.__class__.__name__}.") from exc


async def load_actor(conn: AsyncConnection, user_id: UUID) -> Actor:
    user = await fetch_one(
        conn,
        "SELECT user_id, display_name, account_status FROM users WHERE user_id = :u",
        u=user_id,
    )
    if user is None:
        # A valid login with no Aayra account yet: the app offers independent signup (18+) or tells
        # the person to use their school's invite.
        raise errors.AppError("NO_ACCOUNT", 401, "No Aayra account for this login yet.")
    if user["account_status"] == "INVITED":
        # A valid Supabase token for an invited user proves they accepted the invite and set
        # credentials: activate on first authenticated request.
        await activate_invited_user(conn, user_id)
    elif user["account_status"] != "ACTIVE":
        raise errors.AppError("ACCOUNT_INACTIVE", 403, "This account is not active.")
    rows = await fetch_all(
        conn,
        """SELECT sur.school_id, r.role_code
             FROM school_user_roles sur JOIN roles r ON r.role_id = sur.role_id
            WHERE sur.user_id = :u AND sur.status = 'ACTIVE'""",
        u=user_id,
    )
    roles: dict[UUID, set[str]] = {}
    for r in rows:
        roles.setdefault(r["school_id"], set()).add(r["role_code"])
    return Actor(
        user_id=user_id,
        display_name=user["display_name"],
        roles={k: frozenset(v) for k, v in roles.items()},
    )


async def activate_invited_user(conn: AsyncConnection, user_id: UUID) -> None:
    await execute(
        conn,
        """UPDATE users SET account_status = 'ACTIVE', last_login_at = now()
            WHERE user_id = :u AND account_status = 'INVITED'""",
        u=user_id,
    )
    await execute(
        conn,
        """UPDATE account_activation_state SET state = 'activated'
            WHERE user_id = :u AND state <> 'activated'""",
        u=user_id,
    )


async def current_actor(request: Request) -> Actor:
    """FastAPI dependency: every authenticated endpoint takes `actor: Actor = Depends(...)`."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise errors.unauthenticated()
    claims = decode_token(token)
    try:
        user_id = UUID(claims["sub"])
    except ValueError as exc:
        raise errors.unauthenticated("Token subject is not a user id.") from exc
    async with get_db().begin() as conn:  # begin(): first login may activate the account
        actor = await load_actor(conn, user_id)
    request.state.actor_id = str(actor.user_id)
    return actor


@dataclass(frozen=True)
class Identity:
    """A verified login that may not have an Aayra account yet (self-signup)."""

    user_id: UUID
    email: str | None


async def current_identity(request: Request) -> Identity:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise errors.unauthenticated()
    claims = decode_token(token)
    try:
        user_id = UUID(claims["sub"])
    except ValueError as exc:
        raise errors.unauthenticated("Token subject is not a user id.") from exc
    request.state.actor_id = str(user_id)
    email = claims.get("email")
    return Identity(user_id, email.strip().lower() if isinstance(email, str) and email.strip() else None)


@dataclass(frozen=True)
class StudentContext:
    student_id: UUID
    school_id: UUID
    section_id: UUID
    grade_id: UUID
    academic_year_id: UUID
    enrollment_id: UUID


async def resolve_student_context(conn: AsyncConnection, actor: Actor) -> StudentContext:
    """STUDENT tenancy (contract §1): user -> student_profiles -> the ACTIVE enrollment in an
    ACTIVE academic year -> school/section. Never from identity alone, because transfers,
    rollover and withdrawal change the school relationship while the profile persists."""
    row = await fetch_one(
        conn,
        """SELECT sp.student_id, se.school_id, se.section_id, se.grade_id,
                  se.academic_year_id, se.enrollment_id
             FROM student_profiles sp
             JOIN student_enrollments se ON se.student_id = sp.student_id AND se.status = 'ACTIVE'
             JOIN academic_years ay ON ay.academic_year_id = se.academic_year_id
                                   AND ay.status = 'ACTIVE'
            WHERE sp.user_id = :u AND sp.status = 'ACTIVE'
            ORDER BY ay.start_date DESC
            LIMIT 1""",
        u=actor.user_id,
    )
    if row is None:
        raise errors.enrollment_inactive()
    actor.require_role(row["school_id"], "STUDENT")
    return StudentContext(**row)
