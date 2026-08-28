"""Phase 4.2: audit every PHI access, not just the write paths (P0-8).

The checklist's heads-up is the thesis of this file: *reads* are the ones
that go unlogged, because nothing visibly breaks when they are missing.
The failure only surfaces during a breach investigation, when the question
is "who looked at this patient's record?" and the answer is "we don't
know." A test suite has the same blind spot — every one of these endpoints
already had a green test proving it returns the right data, and every one
of them was silently returning it without leaving a trace.

So the tests below are organised around disclosures, not around routes:
each one drives a real request through the API and then asks what the
trail knows about it afterwards. Three of them are the ones worth keeping
if the rest were deleted:

- `test_no_audit_row_anywhere_contains_phi` — runs a whole consultation
  through the API with distinctive PHI in it and greps every audit row for
  any of it. An audit row outlives the record it describes and (as of this
  phase) cannot be edited or deleted, so PHI written here is written
  permanently.
- `test_polling_an_encounter_does_not_bury_the_human_reads_around_it` —
  the coalescing valve, which is the only place fidelity is traded away.
- `test_access_report_answers_who_looked_at_this_record` — literally the
  question P0-8 exists to make answerable.

The append-only trigger is **not** tested here. It is raw Postgres DDL in
a migration, SQLite has run zero migrations ever, and asserting it from
this file would produce a green test that proves nothing — the exact
failure mode tests/test_postgres_specific.py was written to end. It lives
there, against a real Postgres with the real migration chain.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.audit_log import DEFAULT_AUDIT_LOG_RETENTION_DAYS, AuditLog
from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note
from app.models.patient import Patient
from app.services import audit

_OBJECT_KEY = "encounters/enc-audit/audio/secret-key-abc123.webm"


# --- fixtures ---------------------------------------------------------------


def _clinician(db, *, role: str = "doctor", email: str | None = None) -> Clinician:
    clinician = Clinician(
        email=email or f"{role}@example.com",
        full_name=f"Dr. {role.title()}",
        hashed_password="x",
        role=role,
    )
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician


def _auth(clinician: Clinician) -> dict:
    token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})
    return {"Authorization": f"Bearer {token}"}


def _encounter(db, clinician: Clinician, *, idem: str = "idem-audit-1", patient_id: str | None = None) -> Encounter:
    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key=idem, patient_id=patient_id)
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    return encounter


def _note(db, encounter: Encounter, **kwargs) -> Note:
    note = Note(encounter_id=encounter.id, note_generator_provider="haiku", **kwargs)
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def _actions(db, *, action: str | None = None) -> list[AuditLog]:
    query = db.query(AuditLog)
    if action is not None:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.created_at).all()


def _backdate(db, row: AuditLog, *, seconds: int) -> None:
    """Ages an existing audit row.

    A direct UPDATE, which the production database refuses (that is this
    phase's trigger) — legitimate here only because SQLite has no such
    trigger and there is no other way to simulate the passage of a minute
    without sleeping for one.
    """
    row.created_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    db.add(row)
    db.commit()


# --- reads that were previously invisible ----------------------------------


def test_reading_an_encounter_is_audited(db, client):
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)

    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(clinician))

    rows = _actions(db, action="encounter.read")
    assert len(rows) == 1
    assert rows[0].entity_id == encounter.id
    assert rows[0].actor_clinician_id == clinician.id


def test_a_404_is_not_recorded_as_a_disclosure(db, client):
    """Nothing was read, so nothing is logged. A trail padded with accesses
    that never happened is harder to review, not safer.
    """
    clinician = _clinician(db)

    response = client.get("/api/v1/encounters/does-not-exist", headers=_auth(clinician))

    assert response.status_code == 404
    assert _actions(db, action="encounter.read") == []


def test_listing_loose_sessions_is_audited(db, client):
    clinician = _clinician(db)
    _encounter(db, clinician)

    client.get("/api/v1/encounters/loose", headers=_auth(clinician))

    rows = _actions(db, action="encounter.list.loose")
    assert len(rows) == 1
    # No single subject: the read covered the whole tray.
    assert rows[0].entity_id == "*"
    assert json.loads(rows[0].diff or "{}")["result_count"] == 1


def test_listing_failed_encounters_is_audited(db, client):
    clinician = _clinician(db)

    client.get("/api/v1/encounters/failed", headers=_auth(clinician))

    assert len(_actions(db, action="encounter.list.failed")) == 1


def test_matching_a_patient_is_audited_as_a_read(db, client):
    """A POST that reads. It decrypts and ranks the directory to answer, and
    on a hit it discloses an existing patient's identity — which is a PHI
    access whatever the HTTP verb says.
    """
    clinician = _clinician(db)
    patient = Patient(full_name="Maria Santos Dela Cruz", birthdate=date(1988, 4, 12))
    db.add(patient)
    db.commit()
    db.refresh(patient)

    client.post(
        "/api/v1/patients/match",
        json={"name": "Maria Santos Dela Cruz", "birthdate": "1988-04-12"},
        headers=_auth(clinician),
    )

    rows = _actions(db, action="patient.match")
    assert len(rows) == 1
    assert rows[0].entity_id == patient.id
    assert json.loads(rows[0].diff or "{}")["match_type"] == "exact"


def test_matching_nobody_is_still_audited(db, client):
    """The directory was read to establish that there is no such patient."""
    clinician = _clinician(db)

    client.post(
        "/api/v1/patients/match",
        json={"name": "Nobody At All", "birthdate": "1990-01-01"},
        headers=_auth(clinician),
    )

    rows = _actions(db, action="patient.match")
    assert len(rows) == 1
    assert rows[0].entity_id == "*"
    assert json.loads(rows[0].diff or "{}")["match_type"] == "none"


def test_reading_consent_state_is_audited(db, client):
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)

    client.get(f"/api/v1/consent/{encounter.id}", headers=_auth(clinician))

    rows = _actions(db, action="consent.read")
    assert len(rows) == 1
    assert rows[0].entity_id == encounter.id


# --- changes that were previously invisible --------------------------------


def test_creating_an_encounter_is_audited(db, client):
    clinician = _clinician(db)

    response = client.post(
        "/api/v1/encounters",
        json={"upload_idempotency_key": "idem-new"},
        headers=_auth(clinician),
    )

    rows = _actions(db, action="encounter.create")
    assert len(rows) == 1
    assert rows[0].entity_id == response.json()["id"]


def test_resuming_is_audited_separately_from_creating(db, client):
    """An idempotent retry returns someone's existing recording session. That
    is an access, not a create, and collapsing the two would hide it behind
    an event that did not happen.
    """
    clinician = _clinician(db)
    body = {"upload_idempotency_key": "idem-same"}

    client.post("/api/v1/encounters", json=body, headers=_auth(clinician))
    client.post("/api/v1/encounters", json=body, headers=_auth(clinician))

    assert len(_actions(db, action="encounter.create")) == 1
    assert len(_actions(db, action="encounter.resume")) == 1


def test_linking_a_patient_records_the_previous_value(db, client):
    """Attaching a consultation to the wrong patient is the identity error
    P0-6 exists to prevent. When it happens, "linked to what before?" is
    not reconstructable from the encounter row — it has been overwritten.
    """
    clinician = _clinician(db)
    first = Patient(full_name="Ana Reyes Lim", birthdate=date(2001, 11, 30))
    second = Patient(full_name="Juan Bautista Tan", birthdate=date(1966, 2, 14))
    db.add_all([first, second])
    db.commit()
    db.refresh(first)
    db.refresh(second)
    encounter = _encounter(db, clinician, patient_id=first.id)

    client.post(
        f"/api/v1/encounters/{encounter.id}/link-patient",
        json={"patient_id": second.id},
        headers=_auth(clinician),
    )

    rows = _actions(db, action="encounter.link_patient")
    assert len(rows) == 1
    diff = json.loads(rows[0].diff or "{}")
    assert diff == {"previous_patient_id": first.id, "patient_id": second.id}


def test_editing_a_note_section_is_audited_without_the_text(db, client):
    """The one change to clinical content in the API that wrote no audit row
    before this phase. A NoteRevision was written, but that is not the audit
    trail: it holds the before/after PHI, it dies with the note under
    retention, and the review interface cannot see it.
    """
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)
    note = _note(db, encounter)

    client.patch(
        f"/api/v1/notes/{note.id}",
        json={"section": "assessment", "text": "Rewritten by the doctor."},
        headers=_auth(clinician),
    )

    rows = _actions(db, action="note.edit")
    assert len(rows) == 1
    assert json.loads(rows[0].diff or "{}") == {"section": "assessment"}
    assert "Rewritten" not in (rows[0].diff or "")


def test_retrying_a_failed_encounter_records_what_it_was_retried_from(db, client, monkeypatch):
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", lambda encounter_id: None)
    monkeypatch.setattr("app.tasks.pipeline.run_note_generation", lambda encounter_id: None)
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)
    encounter.pipeline_status = EncounterPipelineStatus.GENERATION_FAILED
    db.add(encounter)
    db.commit()

    client.post(f"/api/v1/encounters/{encounter.id}/retry", headers=_auth(clinician))

    rows = _actions(db, action="encounter.retry")
    assert len(rows) == 1
    assert json.loads(rows[0].diff or "{}")["failed_from"] == "generation_failed"


def test_recording_consent_captures_who_did_it(db, client):
    """The consent ledger deliberately carries no clinician id — it is a
    statement about the patient, not about who typed it. So this row is the
    *only* record of who captured a withdrawal, which is the event that
    triggers deletion of a recording.
    """
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)

    client.post(
        "/api/v1/consent",
        json={
            "encounter_id": encounter.id,
            "event": "withdrawn",
            "participant_roster": ["doctor", "patient"],
            "purposes": ["documentation"],
            "script_language": "en",
        },
        headers=_auth(clinician),
    )

    rows = _actions(db, action="consent.record.withdrawn")
    assert len(rows) == 1
    assert rows[0].actor_clinician_id == clinician.id
    entry = db.query(ConsentLedgerEntry).one()
    # The two records are tied together by id, so a reviewer can get from
    # "someone withdrew consent" to "this clinician did, at this time".
    assert json.loads(rows[0].diff or "{}") == {"consent_ledger_entry_id": entry.id}


# --- the upload path -------------------------------------------------------


def _stub_storage(monkeypatch) -> None:
    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _OBJECT_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", lambda key, ct: "upload-abc")
    monkeypatch.setattr("app.services.storage.presign_part_upload", lambda key, uid, n: "https://example.invalid/part")
    monkeypatch.setattr("app.services.storage.list_uploaded_parts", lambda key, uid: [])
    monkeypatch.setattr("app.services.storage.complete_multipart_upload", lambda key, uid: {})
    monkeypatch.setattr("app.services.storage.head_object", lambda key: {"ContentLength": 1})
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", lambda encounter_id: None)


def test_upload_init_and_complete_are_audited_without_the_object_key(db, client, monkeypatch):
    _stub_storage(monkeypatch)
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)
    db.add(
        ConsentLedgerEntry(
            encounter_id=encounter.id,
            event="given",
            participant_roster="[]",
            purposes="[]",
            script_language="en",
        )
    )
    db.commit()

    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))
    client.post(f"/api/v1/encounters/{encounter.id}/upload/complete", headers=_auth(clinician))

    assert len(_actions(db, action="encounter.upload.init")) == 1
    assert len(_actions(db, action="encounter.upload.complete")) == 1
    # The key is a live pointer at the bytes, and this row outlives them.
    for row in _actions(db):
        assert _OBJECT_KEY not in (row.diff or "")
        assert "secret-key-abc123" not in row.entity_id


def test_an_init_retry_does_not_look_like_a_second_access(db, client, monkeypatch):
    _stub_storage(monkeypatch)
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)

    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    assert len(_actions(db, action="encounter.upload.init")) == 1


def test_minting_a_part_upload_url_is_audited(db, client, monkeypatch):
    """A presigned PUT is a live writable handle on the recording — the same
    class of thing as the playback URL Phase 3 audits, pointed the other way.
    """
    _stub_storage(monkeypatch)
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    client.post(f"/api/v1/encounters/{encounter.id}/upload/parts/1", headers=_auth(clinician))
    client.post(f"/api/v1/encounters/{encounter.id}/upload/parts/2", headers=_auth(clinician))

    # Coalesced: one row for the burst, not one per part.
    assert len(_actions(db, action="encounter.upload.part_url")) == 1


def test_listing_upload_parts_is_deliberately_not_audited(db, client, monkeypatch):
    """The one documented exception in Phase 4.2. It returns part numbers,
    sizes and ETags from S3 — no PHI, no patient, no clinical content — and
    grants nothing. The rule is "log every disclosure of, or capability
    over, PHI", not "log every request"; the latter is the request log, and
    conflating the two is what makes an audit trail unreadable.
    """
    _stub_storage(monkeypatch)
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))
    before = len(_actions(db))

    client.get(f"/api/v1/encounters/{encounter.id}/upload/parts", headers=_auth(clinician))

    assert len(_actions(db)) == before


# --- the coalescing valve ---------------------------------------------------


def test_polling_an_encounter_does_not_bury_the_human_reads_around_it(db, client):
    """The upload queue polls GET /encounters/{id} every 15 seconds until the
    pipeline confirms (apps/web/src/lib/queue). Logged naively, a 20-minute
    transcription writes ~80 identical rows for one recording.
    """
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)

    for _ in range(5):
        client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(clinician))

    assert len(_actions(db, action="encounter.read")) == 1


def test_coalescing_re_records_once_the_window_has_passed(db, client):
    """A continuing access is re-recorded every window, so "this clinician
    had this record open from 09:14 to 10:02" survives. Only the exact hit
    count is lost.
    """
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)

    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(clinician))
    _backdate(db, _actions(db, action="encounter.read")[0], seconds=audit.POLL_COALESCE_SECONDS + 5)
    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(clinician))

    assert len(_actions(db, action="encounter.read")) == 2


def test_coalescing_never_merges_two_different_clinicians(db, client):
    """The point of the log is *who*. A second person reading the same record
    inside the window is the fact most worth keeping, so the window is keyed
    on the actor as well as the entity.
    """
    first = _clinician(db, email="one@example.com")
    second = _clinician(db, email="two@example.com")
    encounter = _encounter(db, first)

    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(first))
    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(second))

    rows = _actions(db, action="encounter.read")
    assert {r.actor_clinician_id for r in rows} == {first.id, second.id}


def test_coalescing_never_merges_two_different_records(db, client):
    first_clinician = _clinician(db)
    a = _encounter(db, first_clinician, idem="idem-a")
    b = _encounter(db, first_clinician, idem="idem-b")

    client.get(f"/api/v1/encounters/{a.id}", headers=_auth(first_clinician))
    client.get(f"/api/v1/encounters/{b.id}", headers=_auth(first_clinician))

    assert {r.entity_id for r in _actions(db, action="encounter.read")} == {a.id, b.id}


def test_writes_are_never_coalesced(db, client):
    """`coalesce_seconds` is opt-in per call site and no write path passes
    it. Two consent events in the same second are two events.
    """
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)
    for event in ("given", "withdrawn"):
        client.post(
            "/api/v1/consent",
            json={
                "encounter_id": encounter.id,
                "event": event,
                "participant_roster": [],
                "purposes": [],
                "script_language": "en",
            },
            headers=_auth(clinician),
        )

    assert len(_actions(db, action="consent.record.given")) == 1
    assert len(_actions(db, action="consent.record.withdrawn")) == 1


# --- the rule that cannot be walked back ------------------------------------


def test_no_audit_row_anywhere_contains_phi(db, client, monkeypatch):
    """Drives a whole consultation through the API with distinctive PHI in
    it, then greps every audit row for any of it.

    This is the test that matters most in the file. An audit row outlives
    the record it describes by years, and as of this phase it cannot be
    edited or deleted — PHI written here is written *permanently*, and no
    later fix can take it back out.
    """
    _stub_storage(monkeypatch)
    name = "Maria Santos Dela Cruz"
    note_text = "Patient reports intermittent chest pain since Tuesday."

    clinician = _clinician(db)
    patient = Patient(full_name=name, birthdate=date(1988, 4, 12))
    db.add(patient)
    db.commit()
    db.refresh(patient)

    headers = _auth(clinician)
    client.post("/api/v1/patients/match", json={"name": name, "birthdate": "1988-04-12"}, headers=headers)
    client.get(f"/api/v1/patients/search?q={name}", headers=headers)
    created = client.post("/api/v1/encounters", json={"upload_idempotency_key": "idem-phi"}, headers=headers).json()
    client.post(f"/api/v1/encounters/{created['id']}/link-patient", json={"patient_id": patient.id}, headers=headers)
    client.post(
        "/api/v1/consent",
        json={
            "encounter_id": created["id"],
            "event": "given",
            "participant_roster": ["doctor", "patient"],
            "purposes": ["documentation"],
            "script_language": "en",
        },
        headers=headers,
    )
    client.post(f"/api/v1/encounters/{created['id']}/upload/init", json={}, headers=headers)
    client.post(f"/api/v1/encounters/{created['id']}/upload/parts/1", headers=headers)
    client.post(f"/api/v1/encounters/{created['id']}/upload/complete", headers=headers)

    encounter = db.get(Encounter, created["id"])
    assert encounter is not None
    note = _note(db, encounter)
    client.get(f"/api/v1/notes/{note.id}", headers=headers)
    client.patch(f"/api/v1/notes/{note.id}", json={"section": "subjective", "text": note_text}, headers=headers)

    rows = _actions(db)
    assert len(rows) > 8  # the walkthrough really did produce a trail
    haystack = " ".join(f"{r.action} {r.entity_type} {r.entity_id} {r.diff or ''}" for r in rows)
    for phi in (name, "Maria", "Santos", "1988-04-12", note_text, "chest pain", _OBJECT_KEY):
        assert phi not in haystack, f"PHI leaked into the audit trail: {phi!r}"


# --- retention (the policy 4.4's purge job reads) ---------------------------


def test_every_row_carries_a_retention_date(db, client):
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)

    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(clinician))

    row = _actions(db, action="encounter.read")[0]
    assert row.retention_expires_at is not None


def test_a_row_written_outside_the_service_still_gets_one(db):
    """The column default, not `audit.record`, is what stamps this — so a
    row written by a future background job or a test cannot slip through
    with a NULL that the purge job would read as "skip forever".
    """
    row = AuditLog(actor_clinician_id=None, action="system.test", entity_type="note", entity_id="x")
    db.add(row)
    db.commit()
    db.refresh(row)

    assert row.retention_expires_at is not None


def test_audit_retention_outlives_phi_retention(db, client):
    """The whole point of a longer window: the trail must still answer "who
    looked at this?" about a recording that has itself already been deleted.
    """
    clinician = _clinician(db)
    encounter = _encounter(db, clinician)
    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(clinician))

    row = _actions(db, action="encounter.read")[0]
    expires = row.retention_expires_at
    if expires.tzinfo is None:  # SQLite drops the offset; the value is UTC
        expires = expires.replace(tzinfo=timezone.utc)
    lifetime_days = (expires - datetime.now(timezone.utc)).days

    assert lifetime_days > get_settings().audio_retention_days
    assert lifetime_days == pytest.approx(DEFAULT_AUDIT_LOG_RETENTION_DAYS, abs=1)


# --- the review interface (P0-8's "reviewable") -----------------------------


def _compliance(db) -> Clinician:
    return _clinician(db, role="compliance", email="compliance@example.com")


def test_review_filters_by_entity(db, client):
    doctor = _clinician(db)
    a = _encounter(db, doctor, idem="idem-a")
    b = _encounter(db, doctor, idem="idem-b")
    client.get(f"/api/v1/encounters/{a.id}", headers=_auth(doctor))
    client.get(f"/api/v1/encounters/{b.id}", headers=_auth(doctor))

    response = client.get(f"/api/v1/audit-logs?entity_type=encounter&entity_id={a.id}", headers=_auth(_compliance(db)))

    assert response.status_code == 200
    assert {row["entity_id"] for row in response.json()} == {a.id}


def test_review_filters_by_action_prefix(db, client):
    """The vocabulary is dotted so a reviewer can ask "everything anyone did
    to notes" without first knowing every exact action string.
    """
    doctor = _clinician(db)
    encounter = _encounter(db, doctor)
    note = _note(db, encounter)
    client.get(f"/api/v1/notes/{note.id}", headers=_auth(doctor))
    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(doctor))

    response = client.get("/api/v1/audit-logs?action_prefix=note.", headers=_auth(_compliance(db)))

    assert [row["action"] for row in response.json()] == ["note.read"]


def test_review_filters_by_time_window(db, client):
    doctor = _clinician(db)
    encounter = _encounter(db, doctor)
    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(doctor))
    _backdate(db, _actions(db, action="encounter.read")[0], seconds=7 * 24 * 3600)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    # `params=`, not an f-string into the path: an ISO-8601 UTC timestamp
    # ends in "+00:00", and a raw "+" in a query string decodes to a space,
    # so the interpolated version 422s. Caught by this test failing that
    # way first — see docs/progress/4.2-audit-logging.md.
    response = client.get("/api/v1/audit-logs", params={"since": cutoff}, headers=_auth(_compliance(db)))

    assert response.status_code == 200
    assert "encounter.read" not in [row["action"] for row in response.json()]


def test_review_paginates_and_reports_the_total(db, client):
    doctor = _clinician(db)
    for i in range(5):
        client.get(f"/api/v1/encounters/{_encounter(db, doctor, idem=f'idem-{i}').id}", headers=_auth(doctor))

    response = client.get("/api/v1/audit-logs?action=encounter.read&limit=2", headers=_auth(_compliance(db)))

    assert len(response.json()) == 2
    # Without the total, a reviewer reading page one has no way to know
    # whether they are looking at the whole story.
    assert response.headers["X-Total-Count"] == "5"


def test_review_exposes_the_retention_date(db, client):
    """A reviewer looking at a nearly-expired row needs to know it is about
    to become unavailable — and it is the value the append-only trigger
    keys off.
    """
    doctor = _clinician(db)
    encounter = _encounter(db, doctor)
    client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(doctor))

    response = client.get("/api/v1/audit-logs?action=encounter.read", headers=_auth(_compliance(db)))

    assert response.json()[0]["retention_expires_at"] is not None


def test_access_report_answers_who_looked_at_this_record(db, client):
    """The question P0-8 exists to make answerable, in one call."""
    first = _clinician(db, email="one@example.com")
    second = _clinician(db, email="two@example.com")
    encounter = _encounter(db, first)
    note = _note(db, encounter)
    client.get(f"/api/v1/notes/{note.id}", headers=_auth(first))
    client.get(f"/api/v1/notes/{note.id}", headers=_auth(second))
    client.get(f"/api/v1/notes/{note.id}", headers=_auth(second))

    response = client.get(
        f"/api/v1/audit-logs/access-report?entity_type=note&entity_id={note.id}",
        headers=_auth(_compliance(db)),
    )

    assert response.status_code == 200
    by_actor = {row["actor_email"]: row for row in response.json()}
    assert by_actor.keys() == {"one@example.com", "two@example.com"}
    assert by_actor["two@example.com"]["access_count"] == 2
    # Names, not UUIDs: a report nobody can read is not "reviewable".
    assert by_actor["one@example.com"]["actor_full_name"] == "Dr. Doctor"
    assert by_actor["one@example.com"]["first_at"] is not None


def test_access_report_keeps_unattributed_access(db, client):
    """An access with no human actor is the *most* interesting line in a
    breach investigation. An inner join on the clinician would silently
    drop exactly those.
    """
    encounter_id = "enc-orphan"
    audit.record(
        db,
        actor_clinician_id=None,
        action="note.read",
        entity_type="encounter",
        entity_id=encounter_id,
    )

    response = client.get(
        f"/api/v1/audit-logs/access-report?entity_type=encounter&entity_id={encounter_id}",
        headers=_auth(_compliance(db)),
    )

    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["actor_clinician_id"] is None
    assert rows[0]["actor_email"] is None


def test_access_report_is_empty_for_a_record_nobody_touched(db, client):
    response = client.get(
        "/api/v1/audit-logs/access-report?entity_type=patient&entity_id=never-touched",
        headers=_auth(_compliance(db)),
    )

    assert response.json() == []


def test_reading_the_audit_log_is_itself_audited(db, client):
    """A review interface that leaves no trace makes the audit trail the one
    surface nobody is accountable for reading — and access logs are read
    precisely when someone is under suspicion.
    """
    compliance = _compliance(db)

    client.get("/api/v1/audit-logs", headers=_auth(compliance))

    rows = _actions(db, action="audit_log.read")
    assert len(rows) == 1
    assert rows[0].actor_clinician_id == compliance.id


def test_the_review_row_is_written_after_the_query_it_describes(db, client):
    """Otherwise the endpoint returns its own audit row, which is both
    confusing and a way for a reviewer to never see an empty result.
    """
    compliance = _compliance(db)

    response = client.get("/api/v1/audit-logs", headers=_auth(compliance))

    assert response.json() == []
    assert len(_actions(db, action="audit_log.read")) == 1


def test_pulling_a_patients_history_shows_up_in_that_patients_history(db, client):
    compliance = _compliance(db)
    patient_id = "pat-under-review"

    client.get(
        f"/api/v1/audit-logs/access-report?entity_type=patient&entity_id={patient_id}",
        headers=_auth(compliance),
    )

    rows = _actions(db, action="audit_log.access_report")
    assert len(rows) == 1
    assert rows[0].entity_type == "patient"
    assert rows[0].entity_id == patient_id


def test_a_reviewers_free_text_filter_is_not_stored(db, client):
    """Same rule `patients.search` follows for its query string: a filter
    value is user-typed text, it can contain a patient name, and this table
    keeps what it is given for years. Only the filter *names* are recorded.
    """
    compliance = _compliance(db)

    client.get("/api/v1/audit-logs?action_prefix=note.&entity_type=note", headers=_auth(compliance))

    row = _actions(db, action="audit_log.read")[0]
    diff = json.loads(row.diff or "{}")
    assert diff == {"filters": ["action_prefix", "entity_type"]}
    # The filter's *value* ("note.") appears nowhere in the row.
    assert "note." not in json.dumps(diff)


def test_a_denied_review_writes_nothing(db, client):
    """403 happens in the dependency, before the route body. Worth stating
    as a known gap rather than leaving to be discovered — see
    docs/progress/4.2-audit-logging.md.
    """
    doctor = _clinician(db)

    response = client.get("/api/v1/audit-logs", headers=_auth(doctor))

    assert response.status_code == 403
    assert _actions(db) == []
