import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import audit_logs, auth, consent, encounters, notes, patients, uploads
from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Phase 1.1: idempotent bucket setup (create-if-missing, default
    encryption, retention + orphan-upload-abort lifecycle rules) at
    startup. Never fails startup — see storage.ensure_bucket_configured's
    own docstring for why a locked-down production IAM role failing this
    is expected, not fatal. Off in tests (S3_PROVISION_BUCKET_ON_STARTUP) —
    see tests/conftest.py for why.
    """
    if settings.s3_provision_bucket_on_startup:
        from app.services.storage import ensure_bucket_configured

        try:
            ensure_bucket_configured()
        except Exception:  # noqa: BLE001 - startup must not crash over bucket admin permissions
            logger.warning("Could not verify/configure the S3 bucket at startup", exc_info=True)
    yield


app = FastAPI(title="Remedy Scribe API", version="0.1.0", lifespan=lifespan)

API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(patients.router, prefix=API_PREFIX)
app.include_router(encounters.router, prefix=API_PREFIX)
app.include_router(uploads.router, prefix=API_PREFIX)
app.include_router(consent.router, prefix=API_PREFIX)
app.include_router(notes.router, prefix=API_PREFIX)
app.include_router(audit_logs.router, prefix=API_PREFIX)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "environment": settings.environment}
