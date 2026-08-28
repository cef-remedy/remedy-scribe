"""Application settings, loaded from environment variables (.env in dev).

See docs/tech-stack.md for the rationale behind each externalized value.
Nothing here should be hardcoded into a literal in application code —
in particular AUDIO_RETENTION_DAYS and NOTE_GENERATOR_PROVIDER, which the
PRD calls out explicitly as values, not code paths, to keep the note-
generator swap and the retention policy operator-configurable.
"""

import base64
import binascii
import hashlib
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def secret_fingerprint(value: str) -> str:
    """A short, non-reversible label for a secret, safe to log or paste
    into a runbook (Phase 4.1). Truncated SHA-256: enough to say "the key
    in staging is not the key in production" without ever writing either
    of them down.
    """
    return hashlib.sha256(value.encode()).hexdigest()[:16]


# Fingerprints of secret material that is PUBLISHED in this repository —
# .env.example and infra/docker-compose.yml. Publishing the development
# secrets rather than having each developer generate their own is the whole
# trick behind 4.1's "production keys never on a developer machine" (P0-8,
# decision 0031): a secret everyone already knows can be denied *by value*,
# which is the only way that bullet becomes enforceable rather than
# aspirational. It also removes the motive for copying real key material
# down to a laptop, because the published one already works locally.
#
# Add a fingerprint here whenever a new dev-default secret is published;
# never remove one, since a leaked-by-design secret stays leaked forever.
_PUBLISHED_DEV_SECRETS: dict[str, str] = {
    "11e86f9fecac0f52": "the key published in apps/api/.env.example",
    "5f03ffdf0ecb300f": "the default value 'change-me-in-every-environment'",
    "9be16e5e60949cba": "published in .env.example and infra/docker-compose.yml",
    "9caf06bb4436cdbf": "the value the test suite uses",
}


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

    # PHI field-level encryption (app/core/security.py:EncryptedString).
    # The key everything is encrypted *with*. Losing it makes every
    # encrypted column permanently unreadable — see docs/runbooks/key-rotation.md.
    phi_encryption_key: str | None = None
    # Keys that may only *decrypt*, comma-separated, most recent first.
    #
    # This is what turns rotation from a lie into a procedure (4.1's ⚠️).
    # scripts/rotate_phi_key.py rewrites every encrypted column under the
    # new key, and that is not instantaneous — so during the rewrite, and
    # after any interruption of it, the database holds a mix of ciphertext
    # under both keys. cryptography's MultiFernet tries each key in turn on
    # decrypt and always encrypts with the first, so listing the outgoing
    # key here keeps the app fully readable throughout (decision 0031).
    #
    # Remove a key once its rotation is *verified* complete. A retired key
    # still listed is a retired key still able to read PHI.
    phi_encryption_key_previous: str = ""

    # Phase 4.1: HSTS. TLS is terminated in front of FastAPI (Phase 5), so
    # the app never sees an https scheme directly — what it can do is emit
    # the header that stops a browser from ever trying http again. Two
    # years, because a max-age shorter than the certificate renewal cycle
    # is a policy that lapses on its own.
    hsts_max_age_seconds: int = 63072000
    # Only set once the whole domain, subdomains included, is genuinely
    # https — includeSubDomains locks out an http-only subdomain for
    # max-age seconds, and there is no fast way back out.
    hsts_include_subdomains: bool = True
    # Off by default: submitting to the browser preload list is close to
    # irreversible, and that is Remedy's call to make once the domain is
    # settled, not a default to inherit accidentally.
    hsts_preload: bool = False

    # Note generation. PRD P0-4 originally specified "Luna primary, Haiku
    # 4.5 configured fallback" — as of the 2026-08-25 planning update
    # (docs/decisions/0021), Haiku is the sole provider; Luna is dropped,
    # not kept dormant. The Literal only accepts today's one real option
    # on purpose (see decision 0011's reasoning against listing values
    # that don't exist yet) — extend it when a second provider is real.
    note_generator_provider: Literal["groq", "haiku"] = "groq"
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
    # Phase 4-era vendor consolidation: note generation moved from
    # Anthropic Haiku to a Groq-hosted model, so the transcript and the
    # audio now go to the same processor rather than two. Production
    # status matters here and is not cosmetic — Groq's BAA/"Covered Cloud
    # Services" definition excludes preview-stage models, so a preview
    # model cannot lawfully carry PHI even though it would work.
    groq_note_model: str = "openai/gpt-oss-120b"

    # Compliance (audio retention is a config value, not a hardcoded default)
    audio_retention_days: int = 90
    # Phase 4.2. Deliberately far longer than PHI retention: the audit log
    # exists to answer "who looked at this record?" during an investigation,
    # and those questions arrive *after* the record itself is gone. An audit
    # trail that expired with its subject could not answer the one question
    # it is kept for. Seven years, matching the usual medical-record
    # retention horizon; the actual figure is Legal's to set.
    audit_log_retention_days: int = 2555

    # Phase 1.5: how long an encounter can sit in a non-terminal,
    # in-flight pipeline_status (uploaded/transcribed) before
    # sweep_stuck_encounters treats it as stuck rather than "still
    # queued" and re-kicks the next stage. See app/tasks/pipeline.py.
    pipeline_stuck_threshold_minutes: int = 30

    # --- Phase 4.1: environment separation, enforced at boot ------------

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"production", "prod"}

    @property
    def phi_previous_key_list(self) -> list[str]:
        return [k.strip() for k in self.phi_encryption_key_previous.split(",") if k.strip()]

    @model_validator(mode="after")
    def _reject_development_secrets_in_production(self) -> "Settings":
        """Refuse to start a production process holding development secrets.

        4.1 asks for "separate keys per environment; production keys never
        on a developer machine". Half of that is enforceable in code and
        this is the half: every secret published in this repository has a
        known fingerprint (_PUBLISHED_DEV_SECRETS), so a production deploy
        that inherited one can be recognised and refused.

        It has to fail at *boot*, not at first use. A PHI key that is only
        validated when a column is first written means the process starts,
        passes its health check, takes traffic, and dies on the first
        patient — with the dev key already committed to whatever rows it
        managed to write first. Wrong-key ciphertext is not recoverable by
        fixing the config afterwards.

        All problems are reported together rather than one per restart:
        a deploy that has one of these wrong usually has several.
        """
        problems: list[str] = []

        # Format-check the key everywhere, not just in production — a
        # malformed key otherwise surfaces as an exception on the first
        # PHI write, which is the worst possible moment to discover it.
        for label, key in [
            ("PHI_ENCRYPTION_KEY", self.phi_encryption_key),
            *((f"PHI_ENCRYPTION_KEY_PREVIOUS[{i}]", k) for i, k in enumerate(self.phi_previous_key_list)),
        ]:
            if key and not _is_well_formed_fernet_key(key):
                problems.append(
                    f"{label} is not a valid Fernet key (expected 32 url-safe "
                    "base64-encoded bytes; generate with "
                    '`python -c "from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"`).'
                )

        # A key listed as both current and previous means a rotation was
        # half-configured. Harmless to decrypt, but it makes the rotation
        # script a no-op that reports success, which is worse than an error.
        if self.phi_encryption_key and self.phi_encryption_key in self.phi_previous_key_list:
            problems.append(
                "PHI_ENCRYPTION_KEY also appears in PHI_ENCRYPTION_KEY_PREVIOUS — "
                "the previous list is for keys being retired, not the active key."
            )

        if self.is_production:
            if not self.phi_encryption_key:
                problems.append(
                    "PHI_ENCRYPTION_KEY is unset. Every PHI column would fail to "
                    "read or write; refusing to start rather than serve a clinic "
                    "an error per patient."
                )

            for label, value in [
                ("PHI_ENCRYPTION_KEY", self.phi_encryption_key),
                ("JWT_SECRET", self.jwt_secret),
                ("S3_SECRET_KEY", self.s3_secret_key),
                *((f"PHI_ENCRYPTION_KEY_PREVIOUS[{i}]", k) for i, k in enumerate(self.phi_previous_key_list)),
            ]:
                if value and (where := _PUBLISHED_DEV_SECRETS.get(secret_fingerprint(value))):
                    problems.append(
                        f"{label} is {where}. It is published in this repository "
                        "and therefore public; it cannot protect production PHI."
                    )

            # The transit half of 4.1. A production deploy terminates TLS in
            # front of the app, so a refresh cookie without Secure is a
            # session credential the browser will happily send over http.
            if not self.refresh_cookie_secure:
                problems.append(
                    "REFRESH_COOKIE_SECURE is false. Production serves over TLS "
                    "(4.1), so the refresh cookie must be Secure-flagged."
                )

            # A production allow-list still naming localhost means the deploy
            # inherited the developer's CORS config, and any other dev-config
            # leak in the same file is likely to have come with it.
            if any(origin.startswith(("http://localhost", "http://127.0.0.1")) for origin in self.cors_origin_list):
                problems.append(
                    "CORS_ALLOW_ORIGINS still contains a localhost origin — "
                    "this looks like a development .env deployed to production."
                )

        if problems:
            raise ValueError(
                "Refusing to start with this configuration "
                f"(ENVIRONMENT={self.environment!r}):\n  - " + "\n  - ".join(problems)
            )
        return self


def _is_well_formed_fernet_key(key: str) -> bool:
    """Fernet keys are exactly 32 bytes, url-safe base64 encoded. Checked
    without constructing a Fernet, because app.core.security imports this
    module and the reverse import would be circular.
    """
    try:
        return len(base64.urlsafe_b64decode(key.encode())) == 32
    except (binascii.Error, ValueError):
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
