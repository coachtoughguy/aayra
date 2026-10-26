"""Runtime configuration. Every value comes from the environment (or a local .env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "local-dev-secret-change-me-at-least-32-bytes"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AAYRA_", env_file=".env", extra="ignore")

    # Direct Postgres connection (SQLAlchemy async URL), e.g.
    # postgresql+asyncpg://postgres:<pw>@db.<ref>.supabase.co:5432/postgres
    database_url: str = "postgresql+asyncpg://postgres@/aayra_dev?host=/tmp&port=54322"

    # Supabase Auth JWT verification. Projects using the legacy shared secret sign with HS256
    # (AAYRA_JWT_SECRET = the project's JWT secret); projects using signing keys sign with ES256/RS256
    # and are verified against {AAYRA_SUPABASE_URL}/auth/v1/.well-known/jwks.json.
    # Set AAYRA_JWT_SECRET to an empty value to disable HS256 entirely.
    jwt_secret: str | None = DEV_JWT_SECRET
    jwt_audience: str = "authenticated"
    jwt_algorithms: tuple[str, ...] = ("HS256", "ES256", "RS256")

    # Serverless deployments (Vercel): no long-lived pool; Supabase's transaction pooler
    # (port 6543) needs prepared-statement caching off. Auto-on when VERCEL is set.
    db_serverless: bool = False

    # Browser origins allowed to call the API (comma separated), e.g. https://aayra-web.vercel.app
    cors_origins: str = ""
    # Shared secret for POST /internal/worker/run (cron / GitHub Actions). Unset = disabled.
    worker_secret: str | None = None
    # Where invite emails send people (the web app), and the login domain for students without email.
    web_app_url: str | None = None
    student_login_domain: str = "students.aayra.invalid"

    # Platform-operator secret for POST /v1/schools (contract §5 "bootstrap-only auth").
    # Unset = school bootstrap disabled.
    bootstrap_key: str | None = None

    # Login provisioning: "stub" (local/tests) or "supabase" (Supabase Auth Admin API; needs
    # AAYRA_SUPABASE_URL + AAYRA_SUPABASE_SERVICE_ROLE_KEY).
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

    # Speech-to-text for VIDEO/AUDIO: "stub" (local/tests) or "openai" (OpenAI audio transcriptions API).
    transcription_provider: str = "stub"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com"
    # Required when transcription_provider=openai: the exact transcription model id (e.g. whisper-1).
    transcription_model: str | None = None
    transcription_timeout_seconds: float = 300.0
    max_media_seconds: int = 4 * 3600

    # Optional: YouTube Data API key, used only to read a video's public title/description/duration.
    youtube_api_key: str | None = None

    environment: str = "local"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    import os

    s = Settings()
    if os.environ.get("VERCEL") and "AAYRA_DB_SERVERLESS" not in os.environ:
        s.db_serverless = True
    if s.environment == "production" and s.jwt_secret == DEV_JWT_SECRET:
        s.jwt_secret = None  # never accept tokens signed with the public development secret
    return s
