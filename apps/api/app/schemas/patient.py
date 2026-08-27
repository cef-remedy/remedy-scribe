from datetime import date, datetime
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


class PatientSearchHit(BaseModel):
    """One ranked result from name-first search (P0-6).

    `birthdate` is returned so the doctor can disambiguate two people with
    the same or similar name — which is the whole reason dedup uses name +
    birthdate together rather than name alone. It is already stored
    unencrypted (see app/models/patient.py) precisely because it has to be
    usable as a discriminator.
    """

    id: str
    full_name: str
    birthdate: date
    score: float
    match_type: Literal["exact", "near"]


class PriorVisitOut(BaseModel):
    """Longitudinal context for the review screen (P0-5). Deliberately only
    assessment and plan: subjective and objective are visit-specific, while
    A and P are what a doctor actually needs carried forward.
    """

    note_id: str
    encounter_id: str
    assessment: str
    plan: str
    signed_at: datetime
