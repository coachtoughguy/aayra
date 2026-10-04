"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.context import install_context
from app.db import get_db
from app.errors import install_error_handlers
from app.routers import health


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
    return app


app = create_app()
