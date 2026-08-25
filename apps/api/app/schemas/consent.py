from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ConsentEntryCreate(BaseModel):
    encounter_id: str
    event: Literal["given", "declined", "withdrawn"]
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
