from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.consent import ConsentEventType


class ConsentEntryCreate(BaseModel):
    encounter_id: str
    event: ConsentEventType
    participant_roster: list[str]
    purposes: list[str]
    script_language: Literal["fil", "en"]


class ConsentEntryOut(BaseModel):
    id: str
    encounter_id: str
    event: str
    script_language: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConsentStateOut(BaseModel):
    """Phase 2.2: lets the client answer "may I start recording?" — P0-1
    requires the app to *block* recording when no consent exists, and it
    cannot block what it cannot see. Deliberately a server read rather
    than client state: a reload mid-encounter loses local state while
    the ledger entry persists, so local state would fail open.

    `can_record` is the same fold the server enforces at upload
    confirmation and at the head of the transcription task (see
    app/services/consent.py) — one definition, three consumers, so the
    client can never believe it may record when the server disagrees.
    """

    encounter_id: str
    can_record: bool
    latest_event: str | None
    script_language: str | None
    entry_count: int
