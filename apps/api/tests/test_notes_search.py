"""`GET /notes/search` -- the "All notes" page.

Before this, the only lists were `/encounters/recent` (a doctor's own last
25), `/loose`, and `/failed` -- nothing let a doctor find a note from two
weeks ago, filter by patient or status, or reach a note a colleague filed.
This is also the first endpoint that lists *notes* (not encounters), and the
first with real pagination and a patient-name filter.

Two things these tests care about that a passing-looking implementation
could still get wrong, both because `Patient.full_name` is encrypted:

- the name filter must actually decrypt-and-match, not silently no-op and
  return everyone (or no one), and
- the audit trail must record that the directory was searched, without
  recording the typed name itself (PHI, same rule `patient.search` follows).
"""

from datetime import date, datetime, timezone

from app.core.security import create_access_token
from app.models.clinician import Clinician
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note, NoteStatus
from app.models.patient import Patient


def _clinician(db, *, email: str, role: str = "doctor") -> Clinician:
    clinician = Clinician(email=email, full_name="Dr. Reyes", hashed_password="x", role=role)
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician


def _auth(clinician: Clinician) -> dict:
    token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})
    return {"Authorization": f"Bearer {token}"}


def _note(
    db,
    clinician: Clinician,
    *,
    idem: str,
    patient: Patient | None = None,
    status_: NoteStatus = NoteStatus.GENERATED,
    created_at: datetime | None = None,
    signed_at: datetime | None = None,
) -> Note:
    encounter = Encounter(
        clinician_id=clinician.id,
        upload_idempotency_key=idem,
        patient_id=patient.id if patient else None,
        pipeline_status=EncounterPipelineStatus.NOTE_GENERATED,
    )
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    if created_at is not None:
        # created_at is server_default'd on insert; back-date it directly
        # for ordering/date-range tests rather than sleeping between inserts.
        encounter.created_at = created_at
        db.add(encounter)
        db.commit()
        db.refresh(encounter)

    note = Note(
        encounter_id=encounter.id,
        note_generator_provider="haiku",
        status=status_,
        assessment="Assessment",
        signed_at=signed_at,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def test_search_returns_notes_newest_first_by_default(db, client):
    doctor = _clinician(db, email="d1@example.com")
    older = _note(db, doctor, idem="old", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = _note(db, doctor, idem="new", created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))

    body = client.get("/api/v1/notes/search", headers=_auth(doctor)).json()

    ids = [row["note_id"] for row in body]
    assert ids.index(newer.id) < ids.index(older.id)


def test_search_reports_unlinked_encounters_as_no_patient(db, client):
    doctor = _clinician(db, email="d2@example.com")
    note = _note(db, doctor, idem="loose")

    body = client.get("/api/v1/notes/search", headers=_auth(doctor)).json()

    row = next(r for r in body if r["note_id"] == note.id)
    assert row["patient_id"] is None
    assert row["patient_name"] is None


def test_search_filters_by_status(db, client):
    doctor = _clinician(db, email="d3@example.com")
    signed = _note(db, doctor, idem="signed", status_=NoteStatus.SIGNED, signed_at=datetime.now(timezone.utc))
    draft = _note(db, doctor, idem="draft", status_=NoteStatus.GENERATED)

    body = client.get("/api/v1/notes/search?status=signed", headers=_auth(doctor)).json()
    ids = {row["note_id"] for row in body}

    assert signed.id in ids
    assert draft.id not in ids


def test_search_filters_by_date_range(db, client):
    doctor = _clinician(db, email="d4@example.com")
    january = _note(db, doctor, idem="jan", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    june = _note(db, doctor, idem="jun", created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))

    body = client.get(
        "/api/v1/notes/search?date_from=2026-05-01T00:00:00Z&date_to=2026-07-01T00:00:00Z",
        headers=_auth(doctor),
    ).json()
    ids = {row["note_id"] for row in body}

    assert june.id in ids
    assert january.id not in ids


def test_search_by_patient_name_decrypts_and_matches(db, client):
    """The one way this could silently pass while broken: filtering on the
    ciphertext directly, which would just return nothing for every query.
    """
    doctor = _clinician(db, email="d5@example.com")
    cruz = Patient(full_name="Maria Santos Dela Cruz", birthdate=date(1988, 4, 12))
    reyes = Patient(full_name="Ana Reyes Lim", birthdate=date(2001, 11, 30))
    db.add_all([cruz, reyes])
    db.commit()
    db.refresh(cruz)
    db.refresh(reyes)

    note_cruz = _note(db, doctor, idem="cruz", patient=cruz)
    note_reyes = _note(db, doctor, idem="reyes", patient=reyes)

    body = client.get("/api/v1/notes/search?q=Maria%20Cruz", headers=_auth(doctor)).json()
    ids = {row["note_id"] for row in body}

    assert note_cruz.id in ids
    assert note_reyes.id not in ids
    assert next(r for r in body if r["note_id"] == note_cruz.id)["patient_name"] == "Maria Santos Dela Cruz"


def test_search_pagination_headers_and_offset(db, client):
    doctor = _clinician(db, email="d6@example.com")
    for i in range(5):
        _note(db, doctor, idem=f"page-{i}", created_at=datetime(2026, 1, i + 1, tzinfo=timezone.utc))

    response = client.get("/api/v1/notes/search?limit=2&offset=0", headers=_auth(doctor))

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert response.headers["X-Total-Count"] == "5"
    assert response.headers["X-Limit"] == "2"
    assert response.headers["X-Offset"] == "0"


def test_search_is_open_to_any_clinician_role(db, client):
    """Decision 0004: note reads (this included) stay open to any
    authenticated clinician for continuity of care -- unlike
    /encounters/recent, which is deliberately doctor-only.
    """
    doctor = _clinician(db, email="d7@example.com")
    compliance = _clinician(db, email="c7@example.com", role="compliance")
    _note(db, doctor, idem="visible-to-all")

    response = client.get("/api/v1/notes/search", headers=_auth(compliance))

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_search_does_not_shadow_note_id_route(db, client):
    """Registration-order regression: `/notes/{note_id}` swallowing the
    literal `/notes/search` path, the same failure mode `encounters.py` and
    `patients.py` both guard against and test for.
    """
    doctor = _clinician(db, email="d8@example.com")

    response = client.get("/api/v1/notes/search", headers=_auth(doctor))

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_search_is_audited_without_recording_the_query_text(db, client):
    from app.models.audit_log import AuditLog

    doctor = _clinician(db, email="d9@example.com")
    _note(db, doctor, idem="audited")

    client.get("/api/v1/notes/search?q=Maria", headers=_auth(doctor))

    entries = db.query(AuditLog).filter(AuditLog.action == "note.search").all()
    assert len(entries) == 1
    assert entries[0].actor_clinician_id == doctor.id
    assert entries[0].entity_id == "*"
    assert "Maria" not in (entries[0].diff or "")
