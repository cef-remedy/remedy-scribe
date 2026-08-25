"""Phase 0.4: fix the type/consistency drift.

The important claim under test here isn't "the Python enum type rejects
a bad value" — Python doesn't enforce type hints at runtime, and nothing
stops code from assigning a plain string to an enum-typed column anyway
(see the other test files, which do exactly that and it works fine,
since our enums are `str` subclasses). The claim under test is stronger:
**the database itself** rejects an invalid value, even via a raw SQL
INSERT that never goes through the ORM at all — the only way "the
database physically cannot hold an invalid value" (the checklist's own
phrase) is actually true rather than aspirational.

Each of these three columns was checked to *not* enforce this before
0.4: `notes.status` looked like an enum (Python-side) but was missing
`create_constraint=True`, which SQLAlchemy 2.0 does not set by default;
`encounters.pipeline_status` and `consent_ledger_entries.event` were
plain String columns with no constraint of any kind.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.clinician import Clinician
from app.models.encounter import Encounter
from app.models.note import Note


def _seed_encounter_with_note(db) -> tuple[Encounter, Note]:
    clinician = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)

    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key="idem-schema-1")
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    note = Note(encounter_id=encounter.id, note_generator_provider="luna")
    db.add(note)
    db.commit()
    db.refresh(note)
    return encounter, note


def test_db_rejects_invalid_note_status_via_raw_sql(db):
    """Retrofit check: this table's CHECK constraint didn't exist before
    0.4 despite the model's docstring claiming it did — this is the
    regression test that would have caught that gap.
    """
    _encounter, note = _seed_encounter_with_note(db)

    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE notes SET status = 'bogus' WHERE id = :id"), {"id": note.id})
        db.commit()
    db.rollback()


def test_db_rejects_invalid_encounter_pipeline_status_via_raw_sql(db):
    encounter, _note = _seed_encounter_with_note(db)

    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE encounters SET pipeline_status = 'bogus' WHERE id = :id"), {"id": encounter.id})
        db.commit()
    db.rollback()


def test_db_rejects_invalid_consent_event_via_raw_sql(db):
    encounter, _note = _seed_encounter_with_note(db)

    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "INSERT INTO consent_ledger_entries "
                "(id, encounter_id, event, participant_roster, purposes, script_language, created_at) "
                "VALUES ('x', :encounter_id, 'bogus', '[]', '[]', 'en', CURRENT_TIMESTAMP)"
            ),
            {"encounter_id": encounter.id},
        )
        db.commit()
    db.rollback()


def test_db_accepts_every_valid_pipeline_status(db):
    """The flip side of the above: the constraint must not be stricter
    than the enum it mirrors — every real value the app writes has to
    still be insertable.
    """
    encounter, _note = _seed_encounter_with_note(db)

    for value in ("recording", "uploaded", "transcribed", "note_generated", "blocked_no_consent"):
        db.execute(text("UPDATE encounters SET pipeline_status = :v WHERE id = :id"), {"v": value, "id": encounter.id})
        db.commit()


def test_confirm_upload_rejects_the_old_query_param_contract(db, client):
    """Locks in the 0.4 contract change: audio_object_key must now be a
    JSON body field. A bare query param with no body is a validation
    error (422), not a silent success — guards against ever drifting
    back to the query-param version by accident.
    """
    from app.core.security import create_access_token

    encounter, _note = _seed_encounter_with_note(db)
    clinician = db.get(Clinician, encounter.clinician_id)
    token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})

    response = client.post(
        f"/api/v1/encounters/{encounter.id}/confirm-upload",
        params={"audio_object_key": "s3://bucket/key"},  # old contract — should no longer work
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
