"""Phase 0.1: the consent gate must be enforced server-side, not just in
the mobile client's UI. These tests exercise both enforcement points —
`confirm_upload` and the head of `transcribe_encounter` — against an
encounter that has no (or no longer active) consent record.
"""

import pytest

from app.core.security import create_access_token
from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.models.encounter import Encounter
from app.services.consent import ConsentNotValidError, assert_consent_valid
from app.tasks.pipeline import transcribe_encounter


def _seed_encounter(db) -> tuple[Encounter, Clinician]:
    clinician = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)

    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key="idem-1")
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    return encounter, clinician


def _ledger_entry(encounter_id: str, event: str) -> ConsentLedgerEntry:
    return ConsentLedgerEntry(
        encounter_id=encounter_id,
        event=event,
        participant_roster="[]",
        purposes="[]",
        script_language="en",
    )


# --- assert_consent_valid, the shared enforcement primitive ---------------


def test_no_ledger_rows_fails_closed(db):
    encounter, _ = _seed_encounter(db)
    with pytest.raises(ConsentNotValidError):
        assert_consent_valid(db, encounter.id)


def test_declined_fails_closed(db):
    encounter, _ = _seed_encounter(db)
    db.add(_ledger_entry(encounter.id, "declined"))
    db.commit()
    with pytest.raises(ConsentNotValidError):
        assert_consent_valid(db, encounter.id)


def test_given_passes(db):
    encounter, _ = _seed_encounter(db)
    db.add(_ledger_entry(encounter.id, "given"))
    db.commit()
    assert_consent_valid(db, encounter.id)  # must not raise


def test_withdrawn_after_given_fails_closed(db):
    encounter, _ = _seed_encounter(db)
    db.add(_ledger_entry(encounter.id, "given"))
    db.commit()
    db.add(_ledger_entry(encounter.id, "withdrawn"))
    db.commit()
    with pytest.raises(ConsentNotValidError):
        assert_consent_valid(db, encounter.id)


def test_re_given_after_withdrawn_passes(db):
    encounter, _ = _seed_encounter(db)
    for event in ("given", "withdrawn", "given"):
        db.add(_ledger_entry(encounter.id, event))
        db.commit()
    assert_consent_valid(db, encounter.id)  # must not raise


# --- enforcement point 1: confirm_upload -----------------------------------


def test_confirm_upload_rejects_encounter_with_no_consent(db, client):
    encounter, clinician = _seed_encounter(db)
    token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})

    response = client.post(
        f"/api/v1/encounters/{encounter.id}/confirm-upload",
        json={"audio_object_key": "s3://bucket/key"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    db.refresh(encounter)
    assert encounter.audio_object_key is None
    assert encounter.pipeline_status == "recording"  # never advanced to "uploaded"


def test_confirm_upload_succeeds_once_consent_given(db, client, monkeypatch):
    # Don't let this test depend on a live Celery broker — confirm_upload
    # kicks off run_pipeline as its last step, which is out of scope here.
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", lambda encounter_id: None)

    encounter, clinician = _seed_encounter(db)
    db.add(_ledger_entry(encounter.id, "given"))
    db.commit()
    token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})

    response = client.post(
        f"/api/v1/encounters/{encounter.id}/confirm-upload",
        json={"audio_object_key": "s3://bucket/key"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["pipeline_status"] == "uploaded"


# --- enforcement point 2: transcribe_encounter (defense in depth) ---------


def test_transcribe_encounter_blocks_without_consent(db):
    encounter, _ = _seed_encounter(db)
    encounter.audio_object_key = "s3://bucket/key"  # simulate confirm_upload's check having been bypassed/raced
    db.add(encounter)
    db.commit()

    result = transcribe_encounter(encounter.id)

    assert result == encounter.id
    db.refresh(encounter)
    assert encounter.pipeline_status == "blocked_no_consent"


def test_transcribe_encounter_blocks_after_withdrawal(db):
    encounter, _ = _seed_encounter(db)
    encounter.audio_object_key = "s3://bucket/key"
    db.add(encounter)
    db.add(_ledger_entry(encounter.id, "given"))
    db.commit()
    db.add(_ledger_entry(encounter.id, "withdrawn"))
    db.commit()

    result = transcribe_encounter(encounter.id)

    assert result == encounter.id
    db.refresh(encounter)
    assert encounter.pipeline_status == "blocked_no_consent"
