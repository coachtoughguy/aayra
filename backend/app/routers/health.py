from fastapi import APIRouter, Depends, Request

from app.auth import Actor, current_actor
from app.db import fetch_one, get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    async with get_db().connect() as conn:
        row = await fetch_one(conn, "SELECT count(*) AS roles FROM roles")
    return {"data": {"status": "ok", "roles": row["roles"] if row else 0}}


@router.get("/v1/me")
async def me(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    """Who am I and which roles do I hold where. Also the simplest auth smoke test."""
    return {
        "data": {
            "user_id": str(actor.user_id),
            "display_name": actor.display_name,
            "roles": {str(s): sorted(r) for s, r in actor.roles.items()},
        },
        "meta": {"request_id": request.state.request_id},
    }
