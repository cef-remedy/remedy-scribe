from datetime import date
from typing import Literal

from pydantic import BaseModel


class PatientLookupRequest(BaseModel):
    """Input to the fuzzy-match flow (P0-6): typed or dictated name + birthdate."""

    name: str
    birthdate: date


class PatientOut(BaseModel):
    id: str
    full_name: str
    birthdate: date

    model_config = {"from_attributes": True}


class PatientMatchResult(BaseModel):
    """P0-6: exact match links silently; near match requires one-tap
    confirmation; no match creates a new record.
    """

    match_type: Literal["exact", "near", "none"]
    patient: PatientOut | None = None
    candidates: list[PatientOut] = []  # populated when match_type == "near"
