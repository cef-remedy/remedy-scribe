from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.encounter import EncounterPipelineStatus
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
    prompt_version: str | None
    signed_by_clinician_id: str | None
    signed_prc_license_number: str | None
    signed_at: datetime | None

    model_config = {"from_attributes": True}


class NoteSearchRow(BaseModel):
    """One row of `GET /notes/search` -- the "All notes" page.

    Deliberately carries no audio link. `AudioPlaybackOut` (see
    app/schemas/grounding.py) is minted only when a doctor asks to hear a
    specific recording, never as part of loading a list or a note --
    bulk-minting one per row here would both contradict that rule and
    write an `encounter.audio.playback_url` audit row for every recording
    on the page, most of which nobody asked to hear.
    """

    note_id: str
    encounter_id: str
    patient_id: str | None
    #: None when the encounter has no patient linked yet (a "loose" note),
    #: not merely when the name failed to decrypt.
    patient_name: str | None
    note_status: NoteStatus
    pipeline_status: EncounterPipelineStatus
    created_at: datetime
    signed_at: datetime | None


class NoteSectionUpdate(BaseModel):
    """Doctor edit to one section before signing (P0-5). Recorded as a
    NoteRevision for the edit-burden metric regardless of how small.
    """

    section: Section
    text: str


class NoteTransitionRequest(BaseModel):
    """Advances the note exactly one step in the state machine
    (P0-5: generated -> filed -> authenticated -> signed, no skipping).
    **Filing additionally requires confirming the patient** (P0-6: identity
    is re-confirmed at the moment a note is filed, not only at recording
    start).
    """

    to_status: NoteStatus
    #: Required for the FILED transition; checked against the encounter's
    #: own patient_id so a stale client cannot file against the wrong person.
    confirmed_patient_id: str | None = None