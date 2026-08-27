from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.clinician import Clinician
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note
from app.schemas.encounter import EncounterCreate, EncounterLinkPatient, EncounterOut

router = APIRouter(prefix="/encounters", tags=["encounters"])

def _encounter_out(db: Session, encounter: Encounter) -> EncounterOut:
    """EncounterOut plus the 1:1 note id (Phase 2.6).

    Resolved here rather than as a relationship on the model so the extra
    query only happens on the read paths that need it, and so `note_id` stays
    a property of the API response rather than of the ORM object.
    """
    note_id = (
        db.query(Note.id).filter(Note.encounter_id == encounter.id).scalar()
        if encounter.pipeline_status
        in (
            EncounterPipelineStatus.NOTE_GENERATED,
            EncounterPipelineStatus.TRANSCRIBED,
        )
        else None
    )
    out = EncounterOut.model_validate(encounter)
    out.note_id = note_id
    return out



# Phase 1.5: the two terminal, dead-lettered statuses — see
# app/tasks/pipeline.py's _mark_stage_failure. Both /failed and /retry
# key off this same pair.
_FAILED_STATUSES = (EncounterPipelineStatus.TRANSCRIPTION_FAILED, EncounterPipelineStatus.GENERATION_FAILED)


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
    return [_encounter_out(db, r) for r in rows]


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


@router.get("/failed", response_model=list[EncounterOut])
def list_failed_encounters(
    db: Session = Depends(get_db),
    # RBAC (0.2): same worklist shape as /loose — a doctor's own failed encounters.
    clinician: Clinician = Depends(require_role("doctor")),
) -> list[EncounterOut]:
    """The dead-letter surfacing this phase's checklist item asks for —
    "after max retries, mark the encounter failed and surface it in the
    app." There is no app yet (Phase 2), so this is the surface: a
    doctor-facing client renders this list and offers /retry per row,
    the same shape /loose already established for a different worklist.
    """
    rows = (
        db.query(Encounter)
        .filter(Encounter.pipeline_status.in_(_FAILED_STATUSES))
        .order_by(Encounter.pipeline_updated_at.desc())
        .all()
    )
    return [_encounter_out(db, r) for r in rows]


@router.post("/{encounter_id}/retry", response_model=EncounterOut)
def retry_pipeline_stage(
    encounter_id: str,
    db: Session = Depends(get_db),
    # RBAC (0.2): choosing to retry a failed encounter is a doctor action.
    clinician: Clinician = Depends(require_role("doctor")),
) -> EncounterOut:
    """The "regenerate note" action the checklist asks for, generalized
    to both dead-letter states rather than just note generation —
    TRANSCRIPTION_FAILED and GENERATION_FAILED are the same shape of
    problem at different stages, and a doctor shouldn't need two
    different buttons for it.

    Re-runs only the failed stage onward, not the whole pipeline from
    scratch: a GENERATION_FAILED encounter already has a real transcript
    (transcription succeeded), so this calls `run_note_generation`
    rather than paying for a second real ASR call that would produce the
    same transcript the first one already did.
    """
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Encounter not found")

    if encounter.pipeline_status not in _FAILED_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Encounter is not in a failed state (currently {encounter.pipeline_status.value})",
        )

    was_transcription_failure = encounter.pipeline_status == EncounterPipelineStatus.TRANSCRIPTION_FAILED
    encounter.pipeline_status = (
        EncounterPipelineStatus.UPLOADED if was_transcription_failure else EncounterPipelineStatus.TRANSCRIBED
    )
    encounter.retry_count = 0
    encounter.last_pipeline_error = None
    encounter.pipeline_updated_at = datetime.now(timezone.utc)
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    # Deferred import: same reasoning as uploads.py's — avoids a hard
    # Celery/Redis dependency at import time for routes that never touch
    # the pipeline.
    from app.tasks.pipeline import run_note_generation, run_pipeline

    if was_transcription_failure:
        run_pipeline(encounter.id)
    else:
        run_note_generation(encounter.id)

    return EncounterOut.model_validate(encounter)


@router.get("/{encounter_id}", response_model=EncounterOut)
def read_encounter(
    encounter_id: str,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> EncounterOut:
    """Phase 2.4: the upload queue polls this to decide when local audio may
    be deleted.

    P0-2 says local audio goes only once the server confirms receipt *and*
    that note generation has begun — and the checklist's heads-up is sharper
    still: "the confirmation the device waits for should be about the
    pipeline, not the bytes." `upload/complete` confirms bytes and enqueues
    work; only `pipeline_status` says whether that work actually ran. So the
    queue waits for this, not for the 200 on complete.

    NOTE ON ROUTE ORDER: this must stay registered *after* `/loose` and
    `/failed`. FastAPI matches in registration order, so a path parameter
    declared before them would swallow both — `/encounters/loose` would
    resolve here with encounter_id="loose" and 404. There is a test for
    exactly that, because the failure is silent and easy to reintroduce by
    tidying this file.
    """
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Encounter not found")
    return _encounter_out(db, encounter)


# Upload confirmation used to live here as `POST /{encounter_id}/confirm-upload`,
# taking a client-supplied `audio_object_key` on faith. Phase 1.1 replaced it
# with the real upload flow in app/api/routes/uploads.py — the server now
# generates the object key itself and only accepts an upload as complete
# once it has verified the object actually exists in storage. See
# docs/decisions/0013.
