"""Phase 2.5: name-first patient search and longitudinal context (P0-6, P0-5).

The search these cover reads *every* patient name in the directory and
decrypts it, so the tests care about two things a passing search could still
get wrong: that it ranks sensibly (a doctor typing a partial name must see
the right person near the top), and that it does not quietly become a
name-only deduplication path — P0-6 is explicit that dedup uses name +
birthdate together.
"""

from datetime import date, datetime, timezone

import pytest

from app.core.security import create_access_token
from app.models.clinician import Clinician
from app.models.encounter import Encounter
from app.models.note import Note, NoteStatus
from app.models.patient import Patient
from app.services.patient_matching import (
    match_patient,
    previous_signed_note,
    search_patients_by_name,
)

_DIRECTORY = [
    ("Maria Santos Dela Cruz", date(1988, 4, 12)),
    ("Maria Santos Gonzales", date(1991, 7, 3)),
    ("Mario Santos Dela Cruz", date(1975, 1, 20)),
    ("Jose Rizal Mercado", date(1861, 6, 19)),
    ("Ana Reyes Lim", date(2001, 11, 30)),
    ("Juan Bautista Tan", date(1966, 2, 14)),
]


def _seed_directory(db) -> dict[str, Patient]:
    created = {}
    for name, birthdate in _DIRECTORY:
        patient = Patient(full_name=name, birthdate=birthdate)
        db.add(patient)
        created[name] = patient
    db.commit()
    for patient in created.values():
        db.refresh(patient)
    return created


def _doctor(db, email: str = "doc@example.com") -> Clinician:
    clinician = Clinician(email=email, full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician


def _auth(clinician: Clinician) -> dict:
    token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})
    return {"Authorization": f"Bearer {token}"}


# --- name-first search ----------------------------------------------------


def test_exact_name_ranks_first_and_is_labelled_exact(db):
    _seed_directory(db)

    hits = search_patients_by_name(db, "Maria Santos Dela Cruz")

    assert hits[0].full_name == "Maria Santos Dela Cruz"
    assert hits[0].match_type == "exact"


def test_search_is_case_and_whitespace_insensitive(db):
    _seed_directory(db)

    hits = search_patients_by_name(db, "  maria   SANTOS  dela cruz ")

    assert hits[0].full_name == "Maria Santos Dela Cruz"
    assert hits[0].match_type == "exact"


def test_a_partial_name_still_finds_the_patient(db):
    """The realistic case: a doctor types what they remember. "Maria Cruz"
    shares tokens with "Maria Santos Dela Cruz" but the full strings differ
    enough that a naive ratio threshold alone would discard it.
    """
    _seed_directory(db)

    hits = search_patients_by_name(db, "Maria Cruz")
    names = [h.full_name for h in hits]

    assert "Maria Santos Dela Cruz" in names


def test_a_typo_still_finds_the_patient(db):
    _seed_directory(db)

    hits = search_patients_by_name(db, "Maria Santos Dela Cruzz")
    names = [h.full_name for h in hits]

    assert "Maria Santos Dela Cruz" in names
    # And it is not labelled exact, because it is not.
    assert next(h for h in hits if h.full_name == "Maria Santos Dela Cruz").match_type == "near"


def test_similar_but_distinct_people_are_both_returned_for_disambiguation(db):
    """"Maria" and "Mario" sharing a surname is exactly the case where the
    doctor must choose, and where returning only the best score would hide
    the mistake. Birthdate comes back with each hit so they can be told
    apart.
    """
    _seed_directory(db)

    hits = search_patients_by_name(db, "Maria Santos Dela Cruz")
    names = [h.full_name for h in hits]

    assert "Maria Santos Dela Cruz" in names
    assert "Mario Santos Dela Cruz" in names
    assert all(h.birthdate is not None for h in hits)


def test_unrelated_names_are_excluded(db):
    _seed_directory(db)

    hits = search_patients_by_name(db, "Maria Santos Dela Cruz")
    names = [h.full_name for h in hits]

    # Sharing no token and no prefix with the query is not a plausible typo.
    assert "Jose Rizal Mercado" not in names
    assert "Juan Bautista Tan" not in names


def test_empty_or_whitespace_query_returns_nothing(db):
    _seed_directory(db)

    assert search_patients_by_name(db, "") == []
    assert search_patients_by_name(db, "     ") == []


def test_results_are_capped(db):
    _seed_directory(db)

    hits = search_patients_by_name(db, "Santos", limit=2)

    assert len(hits) <= 2


def test_ordering_is_stable_across_calls(db):
    """Ties are broken by name rather than left to row order, so the same
    query does not reshuffle candidates between keystrokes — which would
    make the doctor tap the wrong one.
    """
    _seed_directory(db)

    first = [h.id for h in search_patients_by_name(db, "Santos")]
    second = [h.id for h in search_patients_by_name(db, "Santos")]

    assert first == second


def test_search_finds_nothing_in_an_empty_directory(db):
    assert search_patients_by_name(db, "Maria") == []


# --- search does NOT replace name+birthdate dedup (P0-6) ------------------


def test_search_does_not_become_a_dedup_path(db):
    """P0-6: "Deduplication uses name + birthdate together, not name alone."

    Search answers "who might the doctor mean" and takes no birthdate.
    `match_patient` remains the deduplication decision and takes both. This
    asserts the split holds: an identical name with a different birthdate is
    a search hit but NOT a dedup match.
    """
    _seed_directory(db)

    hits = search_patients_by_name(db, "Maria Santos Dela Cruz")
    assert any(h.match_type == "exact" for h in hits)

    # Same name, wrong birthdate -> not the same person.
    result = match_patient(db, "Maria Santos Dela Cruz", date(1999, 9, 9))
    assert result.match_type == "none"

    # Same name, right birthdate -> the same person.
    result = match_patient(db, "Maria Santos Dela Cruz", date(1988, 4, 12))
    assert result.match_type == "exact"


# --- the route ------------------------------------------------------------


def test_search_route_returns_ranked_hits(db, client):
    _seed_directory(db)
    doctor = _doctor(db, email="searcher@example.com")

    response = client.get("/api/v1/patients/search?q=Maria%20Santos", headers=_auth(doctor))

    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 2
    assert body[0]["score"] >= body[-1]["score"]
    assert {"id", "full_name", "birthdate", "score", "match_type"} <= set(body[0])


def test_search_route_rejects_an_empty_query(db, client):
    doctor = _doctor(db, email="searcher@example.com")
    assert client.get("/api/v1/patients/search?q=", headers=_auth(doctor)).status_code == 422


def test_search_route_is_doctor_only(db, client):
    _seed_directory(db)
    compliance = Clinician(
        email="compliance@example.com", full_name="C O", hashed_password="x", role="compliance"
    )
    db.add(compliance)
    db.commit()
    db.refresh(compliance)

    response = client.get("/api/v1/patients/search?q=Maria", headers=_auth(compliance))

    assert response.status_code == 403


def test_search_is_audited_as_a_phi_read(db, client):
    """It decrypts every name in the directory, so it is a PHI read (P0-8).
    Reads are the ones that go unlogged, because nothing visibly breaks.
    """
    from app.models.audit_log import AuditLog

    _seed_directory(db)
    doctor = _doctor(db, email="searcher@example.com")

    client.get("/api/v1/patients/search?q=Maria", headers=_auth(doctor))

    entries = db.query(AuditLog).filter(AuditLog.action == "patient.search").all()
    assert len(entries) == 1
    assert entries[0].actor_clinician_id == doctor.id


# --- prior visit (P0-5 longitudinal context) ------------------------------


def _signed_note_for(db, patient: Patient, clinician: Clinician, *, idem: str, when: datetime) -> Note:
    encounter = Encounter(
        clinician_id=clinician.id, upload_idempotency_key=idem, patient_id=patient.id
    )
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    note = Note(
        encounter_id=encounter.id,
        note_generator_provider="haiku",
        status=NoteStatus.SIGNED,
        assessment="Prior assessment",
        plan="Prior plan",
        signed_by_clinician_id=clinician.id,
        signed_prc_license_number="PRC-1",
        signed_at=when,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def test_prior_visit_returns_the_most_recent_signed_note(db):
    patients = _seed_directory(db)
    doctor = _doctor(db)
    patient = patients["Ana Reyes Lim"]

    _signed_note_for(db, patient, doctor, idem="old", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = _signed_note_for(
        db, patient, doctor, idem="new", when=datetime(2026, 6, 1, tzinfo=timezone.utc)
    )

    assert previous_signed_note(db, patient.id).id == newer.id


def test_prior_visit_ignores_unsigned_notes(db):
    """An unsigned note is an unreviewed AI draft. Presenting one as "the
    last visit" would hand the doctor established-looking history that no
    one has actually vouched for — the opposite of what signing is for.
    """
    patients = _seed_directory(db)
    doctor = _doctor(db)
    patient = patients["Ana Reyes Lim"]

    encounter = Encounter(
        clinician_id=doctor.id, upload_idempotency_key="draft", patient_id=patient.id
    )
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    db.add(
        Note(
            encounter_id=encounter.id,
            note_generator_provider="haiku",
            status=NoteStatus.GENERATED,
            assessment="Unreviewed draft",
        )
    )
    db.commit()

    assert previous_signed_note(db, patient.id) is None


def test_prior_visit_excludes_the_current_encounter(db):
    """Otherwise the note being reviewed could show itself as its own
    history once signed.
    """
    patients = _seed_directory(db)
    doctor = _doctor(db)
    patient = patients["Ana Reyes Lim"]

    note = _signed_note_for(
        db, patient, doctor, idem="current", when=datetime(2026, 6, 1, tzinfo=timezone.utc)
    )

    assert previous_signed_note(db, patient.id, exclude_encounter_id=note.encounter_id) is None


def test_prior_visit_route_returns_null_for_a_first_time_patient(db, client):
    """Null, not 404: a first visit is the normal case, and a 404 would push
    the client into treating it as a failure.
    """
    patients = _seed_directory(db)
    doctor = _doctor(db)

    response = client.get(
        f"/api/v1/patients/{patients['Juan Bautista Tan'].id}/prior-visit", headers=_auth(doctor)
    )

    assert response.status_code == 200
    assert response.json() is None


def test_prior_visit_route_returns_assessment_and_plan_only(db, client):
    patients = _seed_directory(db)
    doctor = _doctor(db)
    patient = patients["Ana Reyes Lim"]
    _signed_note_for(db, patient, doctor, idem="p1", when=datetime(2026, 5, 1, tzinfo=timezone.utc))

    body = client.get(f"/api/v1/patients/{patient.id}/prior-visit", headers=_auth(doctor)).json()

    assert body["assessment"] == "Prior assessment"
    assert body["plan"] == "Prior plan"
    # Subjective and objective are visit-specific; carrying them forward
    # would present last visit's symptoms as if they were today's.
    assert "subjective" not in body
    assert "objective" not in body


@pytest.mark.parametrize("query", ["Dela", "dela cruz", "DELA CRUZ"])
def test_surname_fragments_find_both_dela_cruz_patients(db, query):
    _seed_directory(db)

    names = [h.full_name for h in search_patients_by_name(db, query)]

    assert "Maria Santos Dela Cruz" in names
    assert "Mario Santos Dela Cruz" in names


# --- get-patient-by-id (found by the redesign's completeness audit: --------
# NoteReview re-opens an already-linked encounter with no way to ask who the
# patient is) -----------------------------------------------------------


def test_get_patient_route_returns_the_name_and_birthdate(db, client):
    patients = _seed_directory(db)
    doctor = _doctor(db)
    patient = patients["Ana Reyes Lim"]

    response = client.get(f"/api/v1/patients/{patient.id}", headers=_auth(doctor))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == patient.id
    assert body["full_name"] == "Ana Reyes Lim"
    assert body["birthdate"] == "2001-11-30"


def test_get_patient_route_404s_for_an_unknown_id(db, client):
    doctor = _doctor(db)

    response = client.get("/api/v1/patients/does-not-exist", headers=_auth(doctor))

    assert response.status_code == 404


def test_get_patient_route_is_doctor_only(db, client):
    patients = _seed_directory(db)
    patient = patients["Ana Reyes Lim"]
    compliance = Clinician(
        email="compliance2@example.com", full_name="C O", hashed_password="x", role="compliance"
    )
    db.add(compliance)
    db.commit()
    db.refresh(compliance)

    response = client.get(f"/api/v1/patients/{patient.id}", headers=_auth(compliance))

    assert response.status_code == 403


def test_get_patient_route_is_audited_as_a_phi_read(db, client):
    from app.models.audit_log import AuditLog

    patients = _seed_directory(db)
    doctor = _doctor(db)
    patient = patients["Ana Reyes Lim"]

    client.get(f"/api/v1/patients/{patient.id}", headers=_auth(doctor))

    entries = db.query(AuditLog).filter(AuditLog.action == "patient.read").all()
    assert len(entries) == 1
    assert entries[0].actor_clinician_id == doctor.id
    assert entries[0].entity_id == patient.id


def test_get_patient_route_does_not_shadow_search(db, client):
    """The regression this route could cause if registered in the wrong
    order: `/patients/{patient_id}` matching the literal `/patients/search`
    path and swallowing it.
    """
    _seed_directory(db)
    doctor = _doctor(db, email="searcher2@example.com")

    response = client.get("/api/v1/patients/search?q=Maria%20Santos", headers=_auth(doctor))

    assert response.status_code == 200
    assert isinstance(response.json(), list)
