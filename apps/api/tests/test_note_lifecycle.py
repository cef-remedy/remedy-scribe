import pytest

from app.models.clinician import Clinician
from app.models.encounter import Encounter
from app.models.note import Note, NoteStatus
from app.services.note_lifecycle import (
    InvalidTransitionError,
    SigningRequiresLicenseError,
    transition,
)


def _seed_note(db) -> tuple[Note, Clinician]:
    clinician = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)

    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key="idem-1")
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    note = Note(encounter_id=encounter.id, note_generator_provider="luna")
    db.add(note)
    db.commit()
    db.refresh(note)
    return note, clinician


def test_cannot_skip_states(db):
    note, clinician = _seed_note(db)

    with pytest.raises(InvalidTransitionError):
        transition(db, note, NoteStatus.SIGNED, clinician_id=clinician.id, prc_license_number="PRC-123")


def test_signing_requires_license_number(db):
    note, clinician = _seed_note(db)
    note = transition(db, note, NoteStatus.FILED, clinician_id=clinician.id)
    note = transition(db, note, NoteStatus.AUTHENTICATED, clinician_id=clinician.id)

    with pytest.raises(SigningRequiresLicenseError):
        transition(db, note, NoteStatus.SIGNED, clinician_id=clinician.id)


def test_full_lifecycle_records_signature(db):
    note, clinician = _seed_note(db)

    note = transition(db, note, NoteStatus.FILED, clinician_id=clinician.id)
    note = transition(db, note, NoteStatus.AUTHENTICATED, clinician_id=clinician.id)
    note = transition(db, note, NoteStatus.SIGNED, clinician_id=clinician.id, prc_license_number="PRC-123")

    assert note.status == NoteStatus.SIGNED
    assert note.signed_by_clinician_id == clinician.id
    assert note.signed_prc_license_number == "PRC-123"
    assert note.signed_at is not None
