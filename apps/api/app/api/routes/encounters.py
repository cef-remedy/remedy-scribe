from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.clinician import Clinician
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note
from app.schemas.encounter import EncounterCreate, EncounterLinkPatient, EncounterOut
from app.schemas.grounding import AudioPlaybackOut
from app.services import audit
from app.services.grounding import AudioNotPlayableError, presign_playback_url

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
        # Audited distinctly from a create (Phase 4.2). A resume returns an
        # encounter that may belong to a different clinician's recording —
        # the idempotency key is client-supplied — so "who picked this
        # session back up" is a real access question, and collapsing it
        # into `encounter.create` would hide it behind an event that did
        # not happen.
        audit.record(
            db,
            actor_clinician_id=clinician.id,
            action="encounter.resume",
            entity_type="encounter",
            entity_id=existing.id,
        )
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
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="encounter.create",
        entity_type="encounter",
        entity_id=encounter.id,
        # Whether a recording started already attached to a patient is the
        # one fact about a new encounter worth reconstructing later (P0-6
        # allows recording before identity is known). The patient id itself
        # is a surrogate key, not PHI.
        diff={"patient_linked_at_start": encounter.patient_id is not None},
    )
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
    # A list read is still a read (Phase 4.2). It discloses every unlinked
    # recording in the clinic, not just this doctor's, so "who pulled the
    # loose tray" is worth knowing. entity_id is "*" for the same reason
    # patient.search uses it: there is no single subject, and enumerating
    # the ids returned would write a row proportional to the clinic's
    # backlog on every poll of a tray screen.
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="encounter.list.loose",
        entity_type="encounter",
        entity_id="*",
        diff={"result_count": len(rows)},
    )
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

    previous_patient_id = encounter.patient_id
    encounter.patient_id = payload.patient_id
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    # Phase 4.2: the change that decides *whose* chart a recording ends up
    # in, and it was unlogged. Attaching a consultation to the wrong
    # patient is the identity error P0-6 exists to prevent, and when it
    # happens the first question is "who linked this, and to what before?"
    # — which needs the previous value, hence the before/after shape the
    # `diff` column was designed for. Both are patient ids: surrogate keys,
    # not PHI.
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="encounter.link_patient",
        entity_type="encounter",
        entity_id=encounter.id,
        diff={"previous_patient_id": previous_patient_id, "patient_id": payload.patient_id},
    )
    return EncounterOut.model_validate(encounter)


@router.get("/recent", response_model=list[EncounterOut])
def list_recent_encounters(
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    # RBAC (0.2): a doctor's own recent work, and scoped to them below --
    # unlike /loose, which is deliberately clinic-wide because an unlinked
    # recording is a backlog anyone may need to clear.
    clinician: Clinician = Depends(require_role("doctor")),
) -> list[EncounterOut]:
    """This doctor's recent encounters, newest first.

    Added because its absence was a real hole, found by walking the
    onboarding runbook in a browser rather than by any test: **after filing a
    note there was no way back to it.** The only list endpoints were `/loose`
    (encounters with no patient yet) and `/failed` (dead-lettered ones), so
    the moment a doctor linked a patient the encounter left the only tray that
    showed it, and the note was reachable only by remembering its URL.

    That is the same shape as the Phase 2.6 bug where the review screen was
    unreachable because nothing exposed `note_id` -- the data was correct, the
    screens worked, and no navigation path existed. Unit tests cannot see it,
    because every test addresses an encounter by an id it already holds.

    Scoped to `clinician_id` on purpose. Decision 0004 keeps note *reads* open
    to any clinician for continuity of care, and that stands -- this is a
    "what was I just doing" list, and filling it with colleagues' encounters
    would make it useless for that while disclosing more than the question
    needs.
    """
    rows = (
        db.query(Encounter)
        .filter(Encounter.clinician_id == clinician.id)
        .order_by(Encounter.created_at.desc())
        .limit(limit)
        .all()
    )
    # Same reasoning as /loose: a list read is a read, and the ids are not
    # enumerated into the row because a polled tray screen would write an
    # audit row proportional to the backlog every few seconds.
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="encounter.list.recent",
        entity_type="encounter",
        entity_id="*",
        diff={"result_count": len(rows)},
    )
    return [_encounter_out(db, r) for r in rows]


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
    # Same reasoning as /loose above: a clinic-wide list read.
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="encounter.list.failed",
        entity_type="encounter",
        entity_id="*",
        diff={"result_count": len(rows)},
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

    failed_from = encounter.pipeline_status.value
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

    # Phase 4.2: a retry re-runs ASR and/or the note generator over a
    # patient's consultation — it sends PHI to a third-party processor
    # again, and it can replace a draft a doctor has already read. Both
    # facts belong in the trail, with the state it was retried *from*,
    # which is the only part not reconstructable afterwards (the row is
    # about to be overwritten).
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="encounter.retry",
        entity_type="encounter",
        entity_id=encounter.id,
        diff={"failed_from": failed_from, "resumed_at": encounter.pipeline_status.value},
    )

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

    # Phase 4.2, and the one call site in the API that needed the
    # coalescing valve. This is a real read of an encounter (it discloses
    # the patient linkage, the pipeline state and the note id), so it must
    # be logged — but the upload queue polls it every 15 seconds until the
    # pipeline confirms, so a 20-minute transcription would write ~80
    # identical rows for one recording and bury the human reads either side
    # of them. A 60-second window keeps the first access, keeps the shape
    # of a long polling session, and drops only the repeat count. See
    # docs/decisions/0032.
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="encounter.read",
        entity_type="encounter",
        entity_id=encounter.id,
        coalesce_seconds=audit.POLL_COALESCE_SECONDS,
    )
    return _encounter_out(db, encounter)



@router.get("/{encounter_id}/audio-url", response_model=AudioPlaybackOut)
def read_audio_playback_url(
    encounter_id: str,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> AudioPlaybackOut:
    """Phase 3 (P0-7): a short-lived presigned GET so the grounding UI can
    play audio from a cited timestamp.

    Separate from the grounding read on purpose. A presigned URL is a live,
    playable handle on PHI; minting one every time a doctor opens a note
    would hand out a working link to a recording they may never ask to
    hear. This endpoint is the moment they ask.

    Returns **409, not 404**, when the audio is gone. The encounter exists
    and the caller may read it — what is missing is the recording, which is
    a state problem, and the message says *why* it is missing (deleted at
    the patient's request, retention elapsed, never recorded, or storage
    unreachable). The whole point of this phase's heads-up is that the
    doctor should understand which state they are in.
    """
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Encounter not found")

    try:
        url, expires_in = presign_playback_url(db, encounter)
    except AudioNotPlayableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    # Listening to a consultation recording is a PHI access in its own
    # right, and a more sensitive one than reading the note: the audio is
    # verbatim, including whatever the doctor chose not to write down. The
    # object key is deliberately not recorded — it is a direct pointer to
    # the bytes, and an audit row outlives the retention window of what it
    # points at.
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="encounter.audio.playback_url",
        entity_type="encounter",
        entity_id=encounter.id,
    )
    return AudioPlaybackOut(url=url, expires_in_seconds=expires_in)


# Upload confirmation used to live here as `POST /{encounter_id}/confirm-upload`,
# taking a client-supplied `audio_object_key` on faith. Phase 1.1 replaced it
# with the real upload flow in app/api/routes/uploads.py — the server now
# generates the object key itself and only accepts an upload as complete
# once it has verified the object actually exists in storage. See
# docs/decisions/0013.
