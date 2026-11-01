"""Local-development helpers. Mounted ONLY when AAYRA_ENVIRONMENT=local and not on Vercel:

- `GET /dev/users` + `POST /dev/token`: sign in as any seeded user without Supabase (the web app's
  dev login), using the local development JWT secret.
- `PUT|GET /dev/storage/{key}`: the in-memory stub storage, so the browser can upload and open files
  when AAYRA_STORAGE_PROVIDER=stub.
"""

import time

import jwt
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app import errors
from app.config import get_settings
from app.db import fetch_all, get_db
from app.storage import StubStorage, get_storage

router = APIRouter(prefix="/dev", tags=["dev"], include_in_schema=False)


@router.get("/users")
async def users() -> dict:
    async with get_db().connect() as conn:
        rows = await fetch_all(
            conn,
            """SELECT u.user_id, u.display_name, u.email, array_agg(DISTINCT r.role_code) AS roles
                 FROM users u JOIN school_user_roles sur ON sur.user_id = u.user_id AND sur.status = 'ACTIVE'
                 JOIN roles r ON r.role_id = sur.role_id
                GROUP BY u.user_id, u.display_name, u.email ORDER BY min(r.role_id) DESC, u.display_name LIMIT 60""",
        )
    return {"data": {"users": rows}}


class TokenIn(BaseModel):
    user_id: str


@router.post("/token")
async def token(body: TokenIn) -> dict:
    s = get_settings()
    now = int(time.time())
    claims = {"sub": body.user_id, "aud": s.jwt_audience, "role": "authenticated", "iat": now, "exp": now + 8 * 3600}
    return {"data": {"access_token": jwt.encode(claims, s.jwt_secret, algorithm="HS256")}}


def _stub() -> StubStorage:
    st = get_storage()
    if not isinstance(st, StubStorage):
        raise errors.AppError("NOT_STUB_STORAGE", 404, "Dev storage is only for the stub provider.")
    return st


@router.put("/storage/{key:path}")
async def put_object(key: str, request: Request) -> dict:
    data = await request.body()
    _stub().put(key, len(data), data)
    return {"data": {"object_key": key, "size": len(data)}}


@router.get("/storage/{key:path}")
async def get_object(key: str) -> Response:
    st = _stub()
    if key not in st.objects:
        raise errors.not_found("object", key=key)
    return Response(st.contents.get(key, b""), media_type="application/octet-stream")
