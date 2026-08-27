"""Application settings, loaded from environment variables (.env in dev).

See docs/tech-stack.md for the rationale behind each externalized value.
Nothing here should be hardcoded into a literal in application code —
in particular AUDIO_RETENTION_DAYS and NOTE_GENERATOR_PROVIDER, which the
PRD calls out explicitly as values, not code paths, to keep the note-
generator swap and the retention policy operator-configurable.
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

    # Object storage (Phase 1.1: presigned S3 multipart upload)
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "remedy-scribe-audio"
    s3_access_key: str = "remedy"
    s3_secret_key: str = "remedy-dev-secret"
    s3_presigned_url_expires_seconds: int = 900  # 15 min per part-upload URL
    # Phase 3 (P0-7): the grounding UI's audio playback URL. Shorter than
    # the upload window on purpose — an upload URL goes to a device that
    # already holds the bytes, while this one is a playable handle on PHI.
    # Not shorter than a few minutes, though: a browser streams via Range
    # requests over the life of the URL, so an aggressively short expiry
    # breaks playback mid-passage rather than improving anything.
    s3_playback_url_expires_seconds: int = 300  # 5 min
    # The orphan-upload reaper: how long an incomplete multipart upload
    # survives before the bucket lifecycle rule aborts it automatically.
    s3_abort_incomplete_upload_after_days: int = 2
    # Off in tests (tests/conftest.py) — every TestClient instantiation
    # re-fires the FastAPI startup event, and with no object store
    # actually running, ~50 tests x a handful of network calls each
    # turns into minutes of nothing but connection failures. On by
    # default everywhere else.
    s3_provision_bucket_on_startup: bool = True

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

    # Phase 2.1 (decision 0024: the client is a browser app, not mobile).
    # A native app has no origin and never needed CORS; a browser one does,
    # and gets no useful error without it — the request simply never
    # arrives. Comma-separated rather than a JSON list so a deploy can set
    # it from a plain env var without quoting gymnastics.
    cors_allow_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # The refresh token now travels as an httpOnly cookie rather than in a
    # JSON body the client reads (decision 0024's amendment to 0006). This
    # is the one place a browser is strictly *stronger* than the retired
    # mobile plan: httpOnly means an XSS payload cannot read the token at
    # all, whereas expo-secure-store was always readable by app code.
    refresh_cookie_name: str = "remedy_refresh"
    # False for local http:// dev; MUST be true anywhere real, and paired
    # with samesite=none only if the API and client are cross-site.
    refresh_cookie_secure: bool = False
    refresh_cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    # PHI field-level encryption (app/core/security.py:EncryptedString)
    phi_encryption_key: str | None = None

    # Note generation. PRD P0-4 originally specified "Luna primary, Haiku
    # 4.5 configured fallback" — as of the 2026-08-25 planning update
    # (docs/decisions/0021), Haiku is the sole provider; Luna is dropped,
    # not kept dormant. The Literal only accepts today's one real option
    # on purpose (see decision 0011's reasoning against listing values
    # that don't exist yet) — extend it when a second provider is real.
    note_generator_provider: Literal["haiku"] = "haiku"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-4-5-20251001"
    # Phase 1.4: a word whose ASR confidence falls below this is replaced
    # with an explicit [INAUDIBLE] marker before the transcript ever
    # reaches the prompt — mechanical suppression, not an instruction the
    # model can politely ignore. See app/services/note_generation/haiku.py.
    note_generation_low_confidence_threshold: float = 0.5

    # ASR (Phase 1.3: Groq-hosted Whisper large-v3, not ElevenLabs Scribe v2 —
    # a deliberate switch away from the PRD's named vendor; see
    # docs/decisions/0018 for why, and for the diarization capability this
    # gives up (Whisper does not diarize; ElevenLabs Scribe did)).
    groq_api_key: str | None = None
    groq_whisper_model: str = "whisper-large-v3"

    # Compliance (audio retention is a config value, not a hardcoded default)
    audio_retention_days: int = 90

    # Phase 1.5: how long an encounter can sit in a non-terminal,
    # in-flight pipeline_status (uploaded/transcribed) before
    # sweep_stuck_encounters treats it as stuck rather than "still
    # queued" and re-kicks the next stage. See app/tasks/pipeline.py.
    pipeline_stuck_threshold_minutes: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
