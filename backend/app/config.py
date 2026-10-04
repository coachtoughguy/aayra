"""Runtime configuration. Every value comes from the environment (or a local .env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AAYRA_", env_file=".env", extra="ignore")

    # Direct Postgres connection (SQLAlchemy async URL), e.g.
    # postgresql+asyncpg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres
    database_url: str = "postgresql+asyncpg://postgres@/aayra_dev?host=/tmp&port=54322"

    # Supabase Auth JWT verification (legacy HS256 shared secret).
    jwt_secret: str = "local-dev-secret-change-me-at-least-32-bytes"
    jwt_audience: str = "authenticated"
    jwt_algorithms: tuple[str, ...] = ("HS256",)

    environment: str = "local"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
