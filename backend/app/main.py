"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.context import install_context
from app.db import get_db
from app.errors import install_error_handlers
from app.routers import (
    admin_bootstrap,
    assessments,
    content,
    family_learning,
    feedback,
    health,
    learning,
    lessons,
    students,
    submissions,
    teacher_review,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await get_db().dispose()


def create_app() -> FastAPI:
    logging.basicConfig(level=get_settings().log_level, format="%(message)s")
    app = FastAPI(
        title="Aayra API",
        version="1.2.0",
        description="Implements docs/22 Aayra API & Event Contract v1.2",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    install_context(app)
    app.include_router(health.router)
    app.include_router(admin_bootstrap.router)
    app.include_router(content.router)
    app.include_router(lessons.router)
    app.include_router(students.router)
    app.include_router(learning.router)
    app.include_router(assessments.router)
    app.include_router(teacher_review.router)
    app.include_router(submissions.router)
    app.include_router(feedback.router)
    app.include_router(family_learning.router)
    return app


app = create_app()
