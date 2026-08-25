from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.note import NoteStatus

Section = Literal["assessment", "plan", "subjective", "objective"]


class NoteOut(BaseModel):
    id: str
    encounter_id: str
    status: NoteStatus
    assessment: str
    plan: str
    subjective: str
    objective: str
    note_generator_provider: str
    signed_by_clinician_id: str | None
    signed_prc_license_number: str | None
    signed_at: datetime | None

    model_config = {"from_attributes": True}


class NoteSectionUpdate(BaseModel):
    """Doctor edit to one section before signing (P0-5). Recorded as a
    NoteRevision for the edit-burden metric regardless of how small.
    """

    section: Section
    text: str


class NoteTransitionRequest(BaseModel):
    """Advances the note exactly one step in the state machine
    (P0-5: generated -> filed -> authenticated -> signed, no skipping).
    Signing additionally requires PRC license number.
    """

    to_status: NoteStatus
    prc_license_number: str | None = None
