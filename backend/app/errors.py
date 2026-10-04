"""Stable error taxonomy (contract §13). Clients branch on `code`, never on `message`."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    """A deliberate, client-visible failure with a stable code."""

    def __init__(self, code: str, status: int, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.details = details or {}


# --- Contract §13 codes -------------------------------------------------------------------------
def lesson_not_ready(**d: Any) -> AppError:
    return AppError("LESSON_NOT_READY", 409, "Lesson is not ready for this action.", d)


def generation_version_stale(**d: Any) -> AppError:
    return AppError(
        "GENERATION_VERSION_STALE",
        409,
        "The generated content changed after it was reviewed. Re-fetch and review again.",
        d,
    )


def version_conflict(**d: Any) -> AppError:
    return AppError("VERSION_CONFLICT", 409, "The resource was modified by someone else.", d)


def idempotency_conflict(**d: Any) -> AppError:
    return AppError(
        "IDEMPOTENCY_CONFLICT",
        409,
        "This Idempotency-Key was already used for a different or still-running request.",
        d,
    )


def insufficient_role_scope(**d: Any) -> AppError:
    return AppError("INSUFFICIENT_ROLE_SCOPE", 403, "You do not have access to this resource.", d)


def enrollment_inactive(**d: Any) -> AppError:
    return AppError("ENROLLMENT_INACTIVE", 403, "No active enrollment for the current year.", d)


# --- Generic HTTP-level codes (outside §13, needed by every API) ---------------------------------
def unauthenticated(message: str = "Missing or invalid access token.") -> AppError:
    return AppError("UNAUTHENTICATED", 401, message)


def not_found(entity: str, **d: Any) -> AppError:
    return AppError("NOT_FOUND", 404, f"{entity} not found.", {"entity": entity, **d})


def validation_error(message: str, **d: Any) -> AppError:
    return AppError("VALIDATION_ERROR", 422, message, d)


def _body(code: str, message: str, details: dict[str, Any], request: Request) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": details},
        "meta": {"request_id": getattr(request.state, "request_id", None)},
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details, request), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        body = _body("VALIDATION_ERROR", "Request validation failed.", {"errors": errors}, request)
        return JSONResponse(body, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        request.state.unhandled_error = repr(exc)
        return JSONResponse(_body("INTERNAL", "Unexpected server error.", {}, request), 500)
