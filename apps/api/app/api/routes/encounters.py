from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.clinician import Clinician
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.schemas.encounter import EncounterCreate, EncounterLinkPatient, EncounterOut

router = APIRouter(prefix="/encounters", tags=["encounters"])


@router.post("", response_model=EncounterOut, status_code=201)
def start_or_resume(
    payload: EncounterCreate,
    db: Session = Depends(get_db),
    # RBAC (0.2): starting/resuming a recording is a doctor action.
    clinician: Clinician = Depends(require_role("doctor")),
) -> EncounterOut:
    """Get-or-create on upload_idempotency_key (P0-2: "an idempotency key
    that prevents duplicate notes from a retried upload"). A retry with
    the same key returns the existing encounter instead of creating a
    second one; recording is never blocked on patient_id (P0-6).
    """
    existing = (
        db.query(Encounter).filter(Encounter.upload_idempotency_key == payload.upload_idempotency_key).one_or_none()
    )
    if existing is not None:
        return EncounterOut.model_validate(existing)

    encounter = Encounter(
        patient_id=payload.patient_id,
        clinician_id=clinician.id,
        upload_idempotency_key=payload.upload_idempotency_key,
        pipeline_status=EncounterPipelineStatus.RECORDING,
    )
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    return EncounterOut.model_validate(encounter)


@router.get("/loose", response_model=list[EncounterOut])
def list_loose_sessions(
    db: Session = Depends(get_db),
    # RBAC (0.2): the loose-sessions tray is a doctor's own worklist.
    clinician: Clinician = Depends(require_role("doctor")),
) -> list[EncounterOut]:
    """P0-6: "a persistent 'loose sessions' tray with a one-tap linking
    action" — every encounter with no patient linked yet.
    """
    rows = db.query(Encounter).filter(Encounter.patient_id.is_(None)).order_by(Encounter.created_at.desc()).all()
    return [EncounterOut.model_validate(r) for r in rows]


@router.post("/{encounter_id}/link-patient", response_model=EncounterOut)
def link_patient(
    encounter_id: str,
    payload: EncounterLinkPatient,
    db: Session = Depends(get_db),
    # RBAC (0.2): linking a loose session to a patient is a doctor action.
    clinician: Clinician = Depends(require_role("doctor")),
) -> EncounterOut:
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Encounter not found")

    encounter.patient_id = payload.patient_id
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    return EncounterOut.model_validate(encounter)


# Upload confirmation used to live here as `POST /{encounter_id}/confirm-upload`,
# taking a client-supplied `audio_object_key` on faith. Phase 1.1 replaced it
# with the real upload flow in app/api/routes/uploads.py — the server now
# generates the object key itself and only accepts an upload as complete
# once it has verified the object actually exists in storage. See
# docs/decisions/0013.
