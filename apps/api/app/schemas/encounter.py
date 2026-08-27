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
    # Phase 1.5: what a doctor-facing client needs to render a specific,
    # actionable failure state (P0's "no silent gap in the record") —
    # pipeline_status alone is a state name; these two are what actually
    # happened and how many times it's been tried.
    retry_count: int
    last_pipeline_error: str | None
    #: Phase 2.6. Notes are 1:1 with encounters (Note.encounter_id is
    #: unique), but nothing exposed the id — so the review screen at
    #: /notes/{id} had no way to be reached from a worklist. Found while
    #: writing the end-to-end test for 2.6, which could not navigate to the
    #: screen it was meant to exercise.
    note_id: str | None = None
    audio_retention_expires_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EncounterLinkPatient(BaseModel):
    """One-tap linking action for a loose session (P0-6)."""

    patient_id: str
