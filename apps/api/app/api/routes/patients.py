from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.clinician import Clinician
from app.schemas.patient import PatientLookupRequest, PatientMatchResult, PatientOut
from app.services import audit
from app.services.patient_matching import create_patient, match_patient

router = APIRouter(prefix="/patients", tags=["patients"])


@router.post("/match", response_model=PatientMatchResult)
def match(
    payload: PatientLookupRequest,
    db: Session = Depends(get_db),
    # RBAC (0.2): patient search/match is a doctor action, part of the
    # same recording/identity workflow as encounters.
    clinician: Clinician = Depends(require_role("doctor")),
) -> PatientMatchResult:
    """P0-6: exact match links silently; near match needs a one-tap
    confirmation (client calls POST /patients with the confirmed name if
    the doctor picks a candidate, or accepts the exact match directly);
    no match means the client should create a new record.
    """
    return match_patient(db, payload.name, payload.birthdate)


@router.post("", response_model=PatientOut, status_code=201)
def create(
    payload: PatientLookupRequest,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> PatientOut:
    """Creates a new record (P0-6: "no match creates a new record with
    name + birthdate"). Callers should have already called /match and
    confirmed match_type == "none" — this route does not re-check, since
    the one-tap confirmation UX for a near match also lands here with the
    doctor having explicitly chosen "this is a new patient."
    """
    patient = create_patient(db, payload.name, payload.birthdate)
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="patient.create",
        entity_type="patient",
        entity_id=patient.id,
    )
    return PatientOut.model_validate(patient)
