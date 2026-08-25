from datetime import datetime

from pydantic import BaseModel

from app.models.encounter import EncounterPipelineStatus


class EncounterCreate(BaseModel):
    """Starts (or resumes) an encounter. `upload_idempotency_key` is
    generated client-side once per recording session and replayed on
    every chunk/retry (P0-2) — the same key always resolves to the same
    encounter row.
    """

    upload_idempotency_key: str
    patient_id: str | None = None  # absent -> lands in the loose-sessions tray (P0-6)


class EncounterOut(BaseModel):
    id: str
    patient_id: str | None
    pipeline_status: EncounterPipelineStatus
    audio_retention_expires_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EncounterLinkPatient(BaseModel):
    """One-tap linking action for a loose session (P0-6)."""

    patient_id: str


class ConfirmUploadRequest(BaseModel):
    """Phase 0.4: was a bare `audio_object_key: str` route parameter,
    which FastAPI resolves as a query parameter — an S3 object key
    riding along in the URL/query string (and so in access logs,
    proxies, browser history) rather than the request body where a
    write payload belongs.
    """

    audio_object_key: str
