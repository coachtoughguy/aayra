"""Per-request observability context (contract §12)."""

import json
import logging
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

log = logging.getLogger("aayra.request")


@dataclass(frozen=True)
class RequestContext:
    request_id: UUID
    correlation_id: UUID
    operation: str


def _uuid_or_new(value: str | None) -> UUID:
    try:
        return UUID(value) if value else uuid4()
    except ValueError:
        return uuid4()


def get_context(request: Request) -> RequestContext:
    """FastAPI dependency."""
    return request.state.ctx


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid4()
        correlation_id = (
            _uuid_or_new(request.headers.get("x-correlation-id"))
            if request.headers.get("x-correlation-id")
            else request_id
        )
        request.state.request_id = str(request_id)
        request.state.ctx = RequestContext(
            request_id=request_id,
            correlation_id=correlation_id,
            operation=f"{request.method} {request.url.path}",
        )
        started = time.perf_counter()
        response = await call_next(request)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-Id"] = str(request_id)
        response.headers["X-Correlation-Id"] = str(correlation_id)
        route = request.scope.get("route")
        log.info(
            json.dumps(
                {
                    "request_id": str(request_id),
                    "correlation_id": str(correlation_id),
                    "operation": f"{request.method} {getattr(route, 'path', request.url.path)}",
                    "status": response.status_code,
                    "latency_ms": latency_ms,
                    "actor_id": getattr(request.state, "actor_id", None),
                    "school_id": getattr(request.state, "school_id", None),
                    "idempotency_key": request.headers.get("idempotency-key"),
                    "error": getattr(request.state, "unhandled_error", None),
                }
            )
        )
        return response


def install_context(app: FastAPI) -> None:
    app.add_middleware(RequestContextMiddleware)
