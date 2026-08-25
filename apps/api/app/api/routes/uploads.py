"""Chunked, resumable audio upload (Phase 1.1) — S3 multipart with
presigned part URLs (docs/decisions/0013): this API mints URLs and
tracks metadata; the device PUTs bytes straight to S3/MinIO and never
routes them through this server.

    POST /encounters/{id}/upload/init            -> object_key, upload_id
    POST /encounters/{id}/upload/parts/{n}        -> presigned PUT url
    GET  /encounters/{id}/upload/parts            -> what's already landed
    POST /encounters/{id}/upload/complete         -> finalize + kick pipeline

Supersedes the old `POST /encounters/{id}/confirm-upload`, which trusted
a client-supplied `audio_object_key` with no proof it pointed at
anything real (see docs/decisions/0013).
"""

from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.core.config import get_settings
from app.models.clinician import Clinician
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.schemas.encounter import EncounterOut
from app.schemas.upload import (
    PartUploadUrlResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadPartsStatusResponse,
)
from app.services import storage
from app.services.consent import ConsentNotValidError, assert_consent_valid

router = APIRouter(prefix="/encounters/{encounter_id}/upload", tags=["uploads"])


def _get_encounter_or_404(db: Session, encounter_id: str) -> Encounter:
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Encounter not found")
    return encounter


@router.post("/init", response_model=UploadInitResponse)
def init_upload(
    encounter_id: str,
    payload: UploadInitRequest,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> UploadInitResponse:
    encounter = _get_encounter_or_404(db, encounter_id)

    if encounter.audio_upload_id is not None:
        # Idempotent retry: a phone on clinic wifi may retry init without
        # having seen the first response. Return the same session rather
        # than opening a second, orphaned one in S3.
        return UploadInitResponse(
            object_key=encounter.audio_object_key,
            upload_id=encounter.audio_upload_id,
            min_part_size_bytes=storage.MIN_PART_SIZE_BYTES,
            max_part_number=storage.MAX_PART_NUMBER,
        )

    if encounter.pipeline_status != EncounterPipelineStatus.RECORDING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Upload already completed for this encounter")

    object_key = storage.build_audio_object_key(encounter_id, payload.content_type)
    upload_id = storage.create_multipart_upload(object_key, payload.content_type)

    encounter.audio_object_key = object_key
    encounter.audio_upload_id = upload_id
    db.add(encounter)
    db.commit()

    return UploadInitResponse(
        object_key=object_key,
        upload_id=upload_id,
        min_part_size_bytes=storage.MIN_PART_SIZE_BYTES,
        max_part_number=storage.MAX_PART_NUMBER,
    )


@router.post("/parts/{part_number}", response_model=PartUploadUrlResponse)
def get_part_upload_url(
    encounter_id: str,
    part_number: int = Path(ge=storage.MIN_PART_NUMBER, le=storage.MAX_PART_NUMBER),
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> PartUploadUrlResponse:
    encounter = _get_encounter_or_404(db, encounter_id)
    if encounter.audio_upload_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No upload in progress — call upload/init first")

    settings = get_settings()
    url = storage.presign_part_upload(encounter.audio_object_key, encounter.audio_upload_id, part_number)
    return PartUploadUrlResponse(
        part_number=part_number,
        url=url,
        expires_in_seconds=settings.s3_presigned_url_expires_seconds,
    )


@router.get("/parts", response_model=UploadPartsStatusResponse)
def list_upload_parts(
    encounter_id: str,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> UploadPartsStatusResponse:
    """What a resuming client diffs against — see storage.list_uploaded_parts:
    S3 is the source of truth for "what's already landed," not a
    Postgres table this app would have to keep in sync with it.
    """
    encounter = _get_encounter_or_404(db, encounter_id)
    if encounter.audio_upload_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No upload in progress — call upload/init first")

    parts = storage.list_uploaded_parts(encounter.audio_object_key, encounter.audio_upload_id)
    return UploadPartsStatusResponse(parts=parts)


@router.post("/complete", response_model=EncounterOut)
def complete_upload(
    encounter_id: str,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> EncounterOut:
    """Finalizes the S3 object, then runs the same gate + bookkeeping the
    old confirm_upload did: consent check first (before touching S3 —
    an invalid-consent encounter's multipart upload is left incomplete
    for the lifecycle rule to reap, not finalized), then retention clock,
    pipeline_status, and kicking the pipeline.
    """
    encounter = _get_encounter_or_404(db, encounter_id)

    if encounter.pipeline_status != EncounterPipelineStatus.RECORDING:
        # Idempotent retry: complete already ran (or this encounter never
        # had one to begin with) — either way, nothing left to do here.
        return EncounterOut.model_validate(encounter)

    if encounter.audio_upload_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No upload in progress — call upload/init first")

    try:
        assert_consent_valid(db, encounter_id)
    except ConsentNotValidError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    try:
        storage.complete_multipart_upload(encounter.audio_object_key, encounter.audio_upload_id)
    except storage.NoPartsUploadedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ClientError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Upload finalization failed: {exc}") from exc

    if storage.head_object(encounter.audio_object_key) is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Upload reported complete but the object could not be found in storage"
        )

    settings = get_settings()
    encounter.audio_retention_expires_at = datetime.now(timezone.utc) + timedelta(days=settings.audio_retention_days)
    encounter.pipeline_status = EncounterPipelineStatus.UPLOADED
    encounter.audio_upload_id = None
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    from app.tasks.pipeline import run_pipeline  # deferred import: avoids a hard Celery/Redis dependency at import time for routes that never touch the pipeline

    run_pipeline(encounter.id)
    return EncounterOut.model_validate(encounter)
