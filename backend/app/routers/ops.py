"""Running background jobs on a serverless host (Vercel has no always-on worker process).

Jobs (content processing, lesson preparation, fan-out, scheduled releases, grading) are rows in
Postgres claimed with SKIP LOCKED, so any number of runners can drain them safely. Three triggers:

- `POST /v1/worker/kick`: any signed-in user. The web app calls it after an action that queues work
  (upload finished, lesson created, publish, an answer submitted) and while a screen waits on that
  work. It runs due jobs for up to ~25 s.
- `GET|POST /internal/worker/run`: protected by AAYRA_WORKER_SECRET (or Vercel's CRON_SECRET), for a
  scheduled caller (GitHub Actions every 5 minutes, Vercel Cron, or any uptime pinger). Runs for up
  to ~4 minutes. This is what releases scheduled assignments on time when nobody is online.
- `python -m app.worker`: an always-on process, if the API is hosted somewhere that has one.
"""

import hmac
import os

from fastapi import APIRouter, Depends, Request

from app import errors, worker
from app.auth import Actor, current_actor
from app.config import get_settings

router = APIRouter(tags=["ops"])

KICK_SECONDS = 25.0
SCHEDULED_SECONDS = 240.0


@router.post("/v1/worker/kick")
async def kick(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    ran = await worker.drain(max_jobs=50, time_budget=KICK_SECONDS)
    return {
        "data": {"ran": ran, "waiting": await worker.jobs_waiting()},
        "meta": {"request_id": request.state.request_id},
    }


def _authorized(request: Request) -> bool:
    secrets = [s for s in (get_settings().worker_secret, os.environ.get("CRON_SECRET")) if s]
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return scheme.lower() == "bearer" and any(hmac.compare_digest(token, s) for s in secrets)


@router.api_route("/internal/worker/run", methods=["GET", "POST"])
async def scheduled_run(request: Request) -> dict:
    if not _authorized(request):
        raise errors.unauthenticated("Worker secret required.")
    ran = await worker.drain(time_budget=SCHEDULED_SECONDS)
    return {
        "data": {"ran": ran, "waiting": await worker.jobs_waiting()},
        "meta": {"request_id": request.state.request_id},
    }
