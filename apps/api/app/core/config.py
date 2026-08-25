"""Application settings, loaded from environment variables (.env in dev).

See docs/tech-stack.md for the rationale behind each externalized value.
Nothing here should be hardcoded into a literal in application code —
in particular AUDIO_RETENTION_DAYS and NOTE_GENERATOR_PROVIDER, which the
PRD calls out explicitly as values, not code paths, to keep the Luna/Haiku
swap and the retention policy operator-configurable.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    # Postgres
    database_url: str = "postgresql+psycopg://remedy:remedy@localhost:5432/remedy_scribe"

    # Redis (Celery broker + cache)
    redis_url: str = "redis://localhost:6379/0"

    # Object storage
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "remedy-scribe-audio"
    s3_access_key: str = "remedy"
    s3_secret_key: str = "remedy-dev-secret"

    # Auth
    jwt_secret: str = "change-me-in-every-environment"
    jwt_algorithm: str = "HS256"
    # Phase 0.3: short by design, not a compromise — refresh_token_expire_hours
    # is what actually covers a clinic day; a doctor never sees this expiry
    # because the client refreshes silently. Short access tokens just shrink
    # the window a leaked/logged one is usable in.
    access_token_expire_minutes: int = 15
    refresh_token_expire_hours: int = 12

    # Phase 0.3: login rate limiting / account lockout, both computed from
    # app/models/login_attempt.py rows (see app/services/auth_rate_limit.py).
    login_rate_limit_per_ip_per_minute: int = 10
    login_lockout_threshold: int = 5
    login_lockout_window_minutes: int = 15

    # PHI field-level encryption (app/core/security.py:EncryptedString)
    phi_encryption_key: str | None = None

    # Note generation (PRD P0-4: Luna primary, Haiku 4.5 configured fallback)
    note_generator_provider: Literal["luna", "haiku"] = "luna"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    # ASR
    elevenlabs_api_key: str | None = None

    # Compliance (audio retention is a config value, not a hardcoded default)
    audio_retention_days: int = 90


@lru_cache
def get_settings() -> Settings:
    return Settings()
