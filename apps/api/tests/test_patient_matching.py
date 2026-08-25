from datetime import date

from app.models.patient import Patient
from app.services.patient_matching import match_patient


def _seed(db, name: str, birthdate: date) -> Patient:
    patient = Patient(full_name=name, birthdate=birthdate)
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def test_exact_match_links_silently(db):
    seeded = _seed(db, "Maria Dela Cruz", date(1990, 5, 1))

    result = match_patient(db, "Maria Dela Cruz", date(1990, 5, 1))

    assert result.match_type == "exact"
    assert result.patient.id == seeded.id


def test_near_match_requires_confirmation(db):
    _seed(db, "Maria Dela Cruz", date(1990, 5, 1))

    # Same birthdate, close-but-not-exact name (typo) -> near, not exact.
    result = match_patient(db, "Maria Delacruz", date(1990, 5, 1))

    assert result.match_type == "near"
    assert len(result.candidates) == 1


def test_dedup_requires_name_and_birthdate_together(db):
    # Same name, *different* birthdate must never match — P0-6:
    # "Deduplication uses name + birthdate together, not name alone."
    _seed(db, "Maria Dela Cruz", date(1990, 5, 1))

    result = match_patient(db, "Maria Dela Cruz", date(2001, 1, 1))

    assert result.match_type == "none"
