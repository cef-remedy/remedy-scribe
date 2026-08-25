"""P0-6: "Starting a session accepts a typed or dictated patient name and
fuzzy-matches against the existing directory (exact match links silently;
near match requires one-tap confirmation; no match creates a new record
with name + birthdate)." and "Deduplication uses name + birthdate together,
not name alone."

Uses stdlib difflib rather than pulling in rapidfuzz/thefuzz — the match
set per birthdate is small (patients sharing an exact birthdate), so
O(n) SequenceMatcher against that filtered set is plenty fast, and it
keeps the dependency list in requirements.txt short.
"""

from __future__ import annotations

from datetime import date
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from app.models.patient import Patient
from app.schemas.patient import PatientMatchResult, PatientOut

NEAR_MATCH_THRESHOLD = 0.82


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def match_patient(db: Session, name: str, birthdate: date) -> PatientMatchResult:
    """Birthdate narrows the candidate set first (the "+birthdate" half of
    dedup), then name similarity decides exact vs. near vs. none within
    that set — never matches purely on name.
    """

    candidates = db.query(Patient).filter(Patient.birthdate == birthdate).all()

    exact = next((p for p in candidates if _normalize(p.full_name) == _normalize(name)), None)
    if exact:
        return PatientMatchResult(match_type="exact", patient=PatientOut.model_validate(exact))

    near = [p for p in candidates if _similarity(p.full_name, name) >= NEAR_MATCH_THRESHOLD]
    if near:
        return PatientMatchResult(
            match_type="near",
            candidates=[PatientOut.model_validate(p) for p in near],
        )

    return PatientMatchResult(match_type="none")


def create_patient(db: Session, name: str, birthdate: date) -> Patient:
    patient = Patient(full_name=name, birthdate=birthdate)
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient
