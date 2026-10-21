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

    # Platform-operator secret for POST /v1/schools (contract §5 "bootstrap-only auth").
    # Unset = school bootstrap disabled.
    bootstrap_key: str | None = None

    # Login provisioning: "stub" (local/tests) or "supabase" (not wired yet).
    identity_provider: str = "stub"
    invite_ttl_days: int = 7

    # Object storage: "stub" (local/tests) or "supabase".
    storage_provider: str = "stub"
    storage_bucket: str = "lesson-content"
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None

    # AI models: "stub" (deterministic, local/tests) or "anthropic" (Claude via the Messages API).
    ai_provider: str = "stub"
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    # Required when ai_provider=anthropic: set the exact Claude model id you have access to.
    ai_model: str | None = None
    ai_timeout_seconds: float = 60.0

    environment: str = "local"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
