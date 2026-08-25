"""One Celery chain per encounter: transcribe -> generate_note.

Idempotency (P0-2) is enforced upstream, at the encounter row: routes
resolve `upload_idempotency_key` to a single Encounter via get-or-create
(a unique constraint backs this — see app/models/encounter.py), so a
retried upload never enqueues a second chain for the same recording. Each
task below is additionally idempotent on its own: it no-ops if the work
it would do already exists, so a redelivered Celery message (task_acks_late)
can't double-transcribe or double-generate either.

Phase 1.5 adds the failure-handling half of that same idempotency story:
what happens when a stage doesn't succeed. Two mechanisms, doing two
different jobs —

- **Dead-lettering** (inside each task's `except` block): once a task has
  used up all `max_retries` attempts, the encounter is moved to a
  terminal `*_FAILED` status instead of disappearing into a Celery
  result backend nobody is polling. That status is queryable and
  specific per stage (P0's own "no silent gap in the record"), and
  `retry_pipeline_stage` (called from the `/retry` route) is the
  doctor-triggered way back out of it.
- **`sweep_stuck_encounters`** (Celery Beat, see celery_app.py): catches
  the other failure mode dead-lettering can't — a task that never ran at
  all (broker down, worker pool scaled to zero when `run_pipeline` was
  called) and so never got the chance to except into anything. It looks
  for encounters that stopped making progress, not encounters that
  raised.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note
from app.services.asr import get_asr_provider
from app.services.consent import ConsentNotValidError, assert_consent_valid
from app.services.note_generation import get_note_generator
from app.services.transcripts import load_transcript, persist_transcript
from app.tasks.celery_app import celery_app


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_pipeline(encounter_id: str) -> None:
    """Entry point called by the upload-confirmation route. Kept as a
    plain function (rather than a single mega-task) so it's easy to call
    synchronously in tests without a running Celery worker/broker.
    """
    chain = transcribe_encounter.s(encounter_id) | generate_note.s()
    chain.apply_async()


def run_note_generation(encounter_id: str) -> None:
    """Re-run just `generate_note` — used by `/encounters/{id}/retry` when
    an encounter is `GENERATION_FAILED`. The transcript already exists
    (transcription succeeded); re-running the whole chain would re-pay
    for a real ASR call the first attempt already got right, for no
    reason. Also used by `sweep_stuck_encounters` for the same reason
    when a `TRANSCRIBED` encounter is found stuck.
    """
    generate_note.apply_async(args=[encounter_id])


def _mark_stage_failure(
    db: Session, encounter_id: str, self, exc: Exception, failed_status: EncounterPipelineStatus
) -> bool:
    """Shared by both tasks' `except` blocks. Records the attempt on the
    encounter row and returns True if retries are exhausted (the caller
    should stop retrying and let the exception propagate) or False if the
    caller should call `self.retry(...)` as before.

    `self.request.retries` is the count of retries *already used* — on
    the final allowed attempt it equals `self.max_retries`, one call
    before `self.retry()` would raise `MaxRetriesExceededError` instead
    of actually scheduling another attempt. Checking here, before that
    happens, is what makes the terminal transition deliberate instead of
    incidental.
    """
    db.rollback()
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        return True  # nothing to mark; let the original exception propagate

    exhausted = self.request.retries >= self.max_retries
    encounter.retry_count = self.request.retries + 1
    encounter.last_pipeline_error = str(exc)[:500]
    encounter.pipeline_updated_at = _utcnow()
    if exhausted:
        encounter.pipeline_status = failed_status
    db.add(encounter)
    db.commit()
    return exhausted


@celery_app.task(name="pipeline.transcribe_encounter", bind=True, max_retries=3)
def transcribe_encounter(self, encounter_id: str) -> str:
    db = SessionLocal()
    try:
        encounter = db.get(Encounter, encounter_id)
        if encounter is None:
            raise ValueError(f"Encounter {encounter_id} not found")
        if encounter.pipeline_status in (EncounterPipelineStatus.TRANSCRIBED, EncounterPipelineStatus.NOTE_GENERATED):
            return encounter_id  # already done — redelivered message, no-op

        # Re-checked here, not just at confirm_upload: consent can be
        # withdrawn in the gap between "upload confirmed" and "this task
        # actually runs" (queue backlog, retry delay, worker restart).
        # A withdrawal must stop the pipeline at the next checkpoint.
        assert_consent_valid(db, encounter_id)

        if not encounter.audio_object_key:
            raise ValueError(f"Encounter {encounter_id} has no uploaded audio yet")

        provider = get_asr_provider()
        segments = provider.transcribe(encounter.audio_object_key)
        persist_transcript(
            db, encounter_id, provider_name=provider.provider_name, model_version=provider.model_version, segments=segments
        )

        encounter.pipeline_status = EncounterPipelineStatus.TRANSCRIBED
        encounter.pipeline_updated_at = _utcnow()
        encounter.retry_count = 0  # this stage succeeded — any prior attempts on it no longer matter
        encounter.last_pipeline_error = None
        db.add(encounter)
        db.commit()
        return encounter_id
    except ConsentNotValidError:
        # Not transient — retrying won't make a withdrawn/absent consent
        # valid again. Stop here, terminally, rather than burning retries
        # or (worse) transcribing PHI we're no longer allowed to hold.
        db.rollback()
        encounter = db.get(Encounter, encounter_id)
        if encounter is not None:
            encounter.pipeline_status = EncounterPipelineStatus.BLOCKED_NO_CONSENT
            encounter.pipeline_updated_at = _utcnow()
            db.add(encounter)
            db.commit()
        return encounter_id
    except Exception as exc:  # noqa: BLE001 - retry any transient provider failure
        if _mark_stage_failure(db, encounter_id, self, exc, EncounterPipelineStatus.TRANSCRIPTION_FAILED):
            raise  # retries exhausted — dead-lettered above; nothing left to retry
        raise self.retry(exc=exc, countdown=30) from exc
    finally:
        db.close()


@celery_app.task(name="pipeline.generate_note", bind=True, max_retries=3)
def generate_note(self, encounter_id: str) -> str:
    db = SessionLocal()
    try:
        encounter = db.get(Encounter, encounter_id)
        if encounter is None:
            raise ValueError(f"Encounter {encounter_id} not found")

        existing = db.query(Note).filter(Note.encounter_id == encounter_id).one_or_none()
        if existing is not None:
            return existing.id  # already generated — redelivered message, no-op

        generator = get_note_generator()
        generated = generator.generate(transcript=load_transcript(db, encounter_id))

        note = Note(
            encounter_id=encounter_id,
            assessment=generated.assessment.text,
            plan=generated.plan.text,
            subjective=generated.subjective.text,
            objective=generated.objective.text,
            note_generator_provider=generated.provider,
            prompt_version=generated.prompt_version,
            source_spans=generated.source_spans_json(),
        )
        db.add(note)
        encounter.pipeline_status = EncounterPipelineStatus.NOTE_GENERATED
        encounter.pipeline_updated_at = _utcnow()
        encounter.retry_count = 0
        encounter.last_pipeline_error = None
        db.add(encounter)
        db.commit()
        db.refresh(note)
        return note.id
    except Exception as exc:  # noqa: BLE001
        if _mark_stage_failure(db, encounter_id, self, exc, EncounterPipelineStatus.GENERATION_FAILED):
            raise
        raise self.retry(exc=exc, countdown=30) from exc
    finally:
        db.close()


# Non-terminal statuses a stuck encounter can be found in. Deliberately
# excludes BLOCKED_NO_CONSENT and the two *_FAILED statuses — those are
# terminal by design (retrying them automatically would defeat the point
# of a dead letter: a human, or the /retry route acting on a human's
# behalf, decides what happens next).
_STUCK_STATUSES = (EncounterPipelineStatus.UPLOADED, EncounterPipelineStatus.TRANSCRIBED)


@celery_app.task(name="pipeline.sweep_stuck_encounters")
def sweep_stuck_encounters() -> int:
    """Celery Beat runs this periodically (see celery_app.py). Finds
    encounters that have sat in a non-terminal, in-flight pipeline_status
    past `settings.pipeline_stuck_threshold_minutes` and re-kicks the
    next stage for each.

    This is the failure mode dead-lettering *can't* catch: a task that
    never ran in the first place (the broker was down, the worker pool
    was at zero when `run_pipeline` fired) never reaches an `except`
    block to record anything. Re-kicking is safe specifically because
    both tasks are idempotent no-ops if the work already happened (see
    the module docstring) — if the encounter wasn't actually stuck, just
    slow, this does nothing harmful, it just redelivers a message that
    finds nothing left to do.

    Dispatches via a plain `if`, not a dict built at import time mapping
    status -> `run_pipeline`/`run_note_generation`: a dict built once at
    module load captures those two names' function objects immediately,
    so a test's `monkeypatch.setattr("app.tasks.pipeline.run_pipeline",
    ...)` — which replaces the *module attribute* — would silently miss
    every call already captured in the dict. Referencing the bare names
    inside this function body instead resolves them from the module's
    global namespace at call time, which is exactly what monkeypatch
    relies on.
    """
    db = SessionLocal()
    try:
        threshold = _utcnow() - timedelta(minutes=get_settings().pipeline_stuck_threshold_minutes)
        stuck = (
            db.query(Encounter)
            .filter(
                Encounter.pipeline_status.in_(_STUCK_STATUSES),
                Encounter.pipeline_updated_at < threshold,
            )
            .all()
        )
        for encounter in stuck:
            if encounter.pipeline_status == EncounterPipelineStatus.UPLOADED:
                run_pipeline(encounter.id)
            else:
                run_note_generation(encounter.id)
        return len(stuck)
    finally:
        db.close()
