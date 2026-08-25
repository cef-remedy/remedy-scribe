from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_clinician, get_db
from app.core.config import get_settings
from app.models.clinician import Clinician
from app.models.encounter import Encounter
from app.schemas.encounter import EncounterCreate, EncounterLinkPatient, EncounterOut
from app.services.consent import ConsentNotValidError, assert_consent_valid

router = APIRouter(prefix="/encounters", tags=["encounters"])


@router.post("", response_model=EncounterOut, status_code=201)
def start_or_resume(
    payload: EncounterCreate,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(get_current_clinician),
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
        pipeline_status="recording",
    )
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    return EncounterOut.model_validate(encounter)


@router.get("/loose", response_model=list[EncounterOut])
def list_loose_sessions(
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(get_current_clinician),
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
    clinician: Clinician = Depends(get_current_clinician),
) -> EncounterOut:
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Encounter not found")

    encounter.patient_id = payload.patient_id
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    return EncounterOut.model_validate(encounter)


@router.post("/{encounter_id}/confirm-upload", response_model=EncounterOut)
def confirm_upload(
    encounter_id: str,
    audio_object_key: str,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(get_current_clinician),
) -> EncounterOut:
    """Called once the final chunk lands in object storage. Sets the
    retention clock (Compliance story: retention duration is configurable)
    and kicks off the transcribe -> generate_note pipeline. Local audio on
    the device is only safe to delete once this call succeeds (P0-2).
    """
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Encounter not found")

    try:
        assert_consent_valid(db, encounter_id)
    except ConsentNotValidError as exc:
        # 409, not 403: the clinician is allowed to be here, the ledger
        # just doesn't currently support recording this encounter.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    settings = get_settings()
    encounter.audio_object_key = audio_object_key
    encounter.audio_retention_expires_at = datetime.now(timezone.utc) + timedelta(days=settings.audio_retention_days)
    encounter.pipeline_status = "uploaded"
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    from app.tasks.pipeline import run_pipeline  # deferred import: avoids a hard Celery/Redis dependency at import time for routes that never touch the pipeline

    run_pipeline(encounter.id)
    return EncounterOut.model_validate(encounter)
