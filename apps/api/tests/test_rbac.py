"""Phase 0.2: `require_role` was defined in app/api/deps.py and used on
zero endpoints — a dependency written but never attached, which reads
like coverage in review and provides none. These tests exercise the
policy now wired onto routes: clinical write actions (recording,
consent, patient identity, note editing/signing) are "doctor"-only;
audit-log reads are "compliance"/"admin"-only; note *reads* stay open
to any authenticated clinician (see docs/decisions/0004).
"""

import pytest

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.clinician import Clinician
from app.models.encounter import Encounter
from app.models.note import Note


def _seed_clinician(db, *, role: str) -> Clinician:
    clinician = Clinician(
        email=f"{role}@example.com",
        full_name=f"Test {role.title()}",
        hashed_password="x",
        role=role,
    )
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician


def _token_for(clinician: Clinician) -> str:
    return create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})


def _seed_note(db, clinician: Clinician) -> Note:
    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key=f"idem-{clinician.id}")
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    note = Note(encounter_id=encounter.id, note_generator_provider="haiku")
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


# --- notes: doctor-only writes, any-clinician reads ------------------------


def test_compliance_cannot_patch_note(db, client):
    doctor = _seed_clinician(db, role="doctor")
    note = _seed_note(db, doctor)
    compliance = _seed_clinician(db, role="compliance")

    response = client.patch(
        f"/api/v1/notes/{note.id}",
        json={"section": "assessment", "text": "edited"},
        headers={"Authorization": f"Bearer {_token_for(compliance)}"},
    )

    assert response.status_code == 403


def test_doctor_can_patch_note(db, client):
    doctor = _seed_clinician(db, role="doctor")
    note = _seed_note(db, doctor)

    response = client.patch(
        f"/api/v1/notes/{note.id}",
        json={"section": "assessment", "text": "edited"},
        headers={"Authorization": f"Bearer {_token_for(doctor)}"},
    )

    assert response.status_code == 200
    assert response.json()["assessment"] == "edited"


def test_compliance_can_read_note(db, client):
    doctor = _seed_clinician(db, role="doctor")
    note = _seed_note(db, doctor)
    compliance = _seed_clinician(db, role="compliance")

    response = client.get(
        f"/api/v1/notes/{note.id}",
        headers={"Authorization": f"Bearer {_token_for(compliance)}"},
    )

    assert response.status_code == 200  # reads are open; accountability is via audit.record, not a block


def test_compliance_cannot_sign_note(db, client):
    doctor = _seed_clinician(db, role="doctor")
    note = _seed_note(db, doctor)
    compliance = _seed_clinician(db, role="compliance")

    response = client.post(
        f"/api/v1/notes/{note.id}/transition",
        json={"to_status": "filed"},
        headers={"Authorization": f"Bearer {_token_for(compliance)}"},
    )

    assert response.status_code == 403


# --- audit logs: compliance/admin-only reads -------------------------------


def test_doctor_cannot_read_audit_log(db, client):
    doctor = _seed_clinician(db, role="doctor")
    db.add(AuditLog(actor_clinician_id=doctor.id, action="note.read", entity_type="note", entity_id="x"))
    db.commit()

    response = client.get("/api/v1/audit-logs", headers={"Authorization": f"Bearer {_token_for(doctor)}"})

    assert response.status_code == 403


@pytest.mark.parametrize("role", ["compliance", "admin"])
def test_compliance_and_admin_can_read_audit_log(db, client, role):
    clinician = _seed_clinician(db, role=role)
    db.add(AuditLog(actor_clinician_id=clinician.id, action="note.read", entity_type="note", entity_id="x"))
    db.commit()

    response = client.get("/api/v1/audit-logs", headers={"Authorization": f"Bearer {_token_for(clinician)}"})

    assert response.status_code == 200
    assert len(response.json()) == 1


# --- recording workflow: doctor-only writes --------------------------------


def test_compliance_cannot_start_encounter(db, client):
    compliance = _seed_clinician(db, role="compliance")

    response = client.post(
        "/api/v1/encounters",
        json={"upload_idempotency_key": "idem-rbac-1"},
        headers={"Authorization": f"Bearer {_token_for(compliance)}"},
    )

    assert response.status_code == 403


def test_doctor_can_start_encounter(db, client):
    doctor = _seed_clinician(db, role="doctor")

    response = client.post(
        "/api/v1/encounters",
        json={"upload_idempotency_key": "idem-rbac-2"},
        headers={"Authorization": f"Bearer {_token_for(doctor)}"},
    )

    assert response.status_code == 201


def test_compliance_cannot_retry_failed_encounter(db, client):
    """Phase 1.5: /retry is the same shape of doctor-only pipeline action
    as starting an encounter — reuses the identical `require_role`
    dependency, but worth its own regression since it's a new route.
    """
    from app.models.encounter import EncounterPipelineStatus

    doctor = _seed_clinician(db, role="doctor")
    compliance = _seed_clinician(db, role="compliance")

    encounter = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key="idem-rbac-retry",
        pipeline_status=EncounterPipelineStatus.GENERATION_FAILED,
    )
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    response = client.post(
        f"/api/v1/encounters/{encounter.id}/retry",
        headers={"Authorization": f"Bearer {_token_for(compliance)}"},
    )

    assert response.status_code == 403
