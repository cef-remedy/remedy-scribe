import pytest

from app.models.clinician import Clinician
from app.models.encounter import Encounter
from app.models.note import Note, NoteStatus
from app.models.patient import Patient
from app.services.note_lifecycle import (
    InvalidTransitionError,
    PatientIdentityNotConfirmedError,
    SigningRequiresLicenseError,
    transition,
)


def _seed_note(db, *, link_patient: bool = True) -> tuple[Note, Clinician, Patient | None]:
    """Phase 2.5 changed this fixture's contract: filing now requires the
    encounter to be linked to a patient AND the caller to confirm which one
    (P0-6). `link_patient=False` exercises the unlinked case.
    """
    from datetime import date

    clinician = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)

    patient = None
    if link_patient:
        patient = Patient(full_name="Maria Santos Dela Cruz", birthdate=date(1988, 4, 12))
        db.add(patient)
        db.commit()
        db.refresh(patient)

    encounter = Encounter(
        clinician_id=clinician.id,
        upload_idempotency_key="idem-1",
        patient_id=patient.id if patient else None,
    )
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    note = Note(encounter_id=encounter.id, note_generator_provider="haiku")
    db.add(note)
    db.commit()
    db.refresh(note)
    return note, clinician, patient


def test_cannot_skip_states(db):
    note, clinician, _patient = _seed_note(db)

    with pytest.raises(InvalidTransitionError):
        transition(db, note, NoteStatus.SIGNED, clinician_id=clinician.id, prc_license_number="PRC-123")


def test_signing_requires_license_number(db):
    note, clinician, patient = _seed_note(db)
    note = transition(
        db, note, NoteStatus.FILED, clinician_id=clinician.id, confirmed_patient_id=patient.id
    )
    note = transition(db, note, NoteStatus.AUTHENTICATED, clinician_id=clinician.id)

    with pytest.raises(SigningRequiresLicenseError):
        transition(db, note, NoteStatus.SIGNED, clinician_id=clinician.id)


def test_full_lifecycle_records_signature(db):
    note, clinician, patient = _seed_note(db)

    note = transition(
        db, note, NoteStatus.FILED, clinician_id=clinician.id, confirmed_patient_id=patient.id
    )
    note = transition(db, note, NoteStatus.AUTHENTICATED, clinician_id=clinician.id)
    note = transition(db, note, NoteStatus.SIGNED, clinician_id=clinician.id, prc_license_number="PRC-123")

    assert note.status == NoteStatus.SIGNED
    assert note.signed_by_clinician_id == clinician.id
    assert note.signed_prc_license_number == "PRC-123"
    assert note.signed_at is not None


# --- Phase 2.5: identity is re-confirmed at filing (P0-6) -----------------
#
# Filing is the moment a note joins a patient's permanent record, so it is
# the last cheap chance to catch a mis-linked recording. After that the note
# is in the wrong person's history.


def test_filing_requires_the_encounter_to_be_linked_to_a_patient(db):
    note, clinician, _ = _seed_note(db, link_patient=False)

    with pytest.raises(PatientIdentityNotConfirmedError, match="not linked to a patient"):
        transition(db, note, NoteStatus.FILED, clinician_id=clinician.id, confirmed_patient_id="anything")


def test_filing_requires_an_explicit_confirmation(db):
    """A linked encounter is not enough. The caller must NAME the patient,
    because reading encounter.patient_id and trusting it would make this a
    formality rather than a confirmation.
    """
    note, clinician, _patient = _seed_note(db)

    with pytest.raises(PatientIdentityNotConfirmedError, match="requires confirming"):
        transition(db, note, NoteStatus.FILED, clinician_id=clinician.id)


def test_filing_rejects_a_mismatched_confirmation(db):
    """The case this exists to catch: a stale client showing the previous
    patient. Silently preferring either side is how a note lands in the
    wrong record, so the mismatch is an error rather than a resolution.
    """
    from datetime import date

    note, clinician, _patient = _seed_note(db)
    someone_else = Patient(full_name="Jose Rizal Mercado", birthdate=date(1861, 6, 19))
    db.add(someone_else)
    db.commit()
    db.refresh(someone_else)

    with pytest.raises(PatientIdentityNotConfirmedError, match="does not match"):
        transition(
            db, note, NoteStatus.FILED, clinician_id=clinician.id, confirmed_patient_id=someone_else.id
        )

    db.refresh(note)
    assert note.status == NoteStatus.GENERATED  # unchanged


def test_confirmation_is_only_required_at_filing(db):
    """Later transitions do not re-ask: identity was confirmed when the note
    was filed, and re-prompting at every step would train the doctor to tap
    through it.
    """
    note, clinician, patient = _seed_note(db)
    note = transition(
        db, note, NoteStatus.FILED, clinician_id=clinician.id, confirmed_patient_id=patient.id
    )
    note = transition(db, note, NoteStatus.AUTHENTICATED, clinician_id=clinician.id)
    note = transition(db, note, NoteStatus.SIGNED, clinician_id=clinician.id, prc_license_number="PRC-1")

    assert note.status == NoteStatus.SIGNED
