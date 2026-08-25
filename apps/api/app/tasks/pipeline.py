"""One Celery chain per encounter: transcribe -> generate_note.

Idempotency (P0-2) is enforced upstream, at the encounter row: routes
resolve `upload_idempotency_key` to a single Encounter via get-or-create
(a unique constraint backs this — see app/models/encounter.py), so a
retried upload never enqueues a second chain for the same recording. Each
task below is additionally idempotent on its own: it no-ops if the work
it would do already exists, so a redelivered Celery message (task_acks_late)
can't double-transcribe or double-generate either.
"""

from __future__ import annotations

from app.db.session import SessionLocal
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note
from app.services.asr import get_asr_provider
from app.services.consent import ConsentNotValidError, assert_consent_valid
from app.services.note_generation import get_note_generator
from app.services.transcripts import load_transcript, persist_transcript
from app.tasks.celery_app import celery_app


def run_pipeline(encounter_id: str) -> None:
    """Entry point called by the upload-confirmation route. Kept as a
    plain function (rather than a single mega-task) so it's easy to call
    synchronously in tests without a running Celery worker/broker.
    """
    chain = transcribe_encounter.s(encounter_id) | generate_note.s()
    chain.apply_async()


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
        persist_transcript(db, encounter_id, provider_name=provider.provider_name, segments=segments)

        encounter.pipeline_status = EncounterPipelineStatus.TRANSCRIBED
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
            db.add(encounter)
            db.commit()
        return encounter_id
    except Exception as exc:  # noqa: BLE001 - retry any transient provider failure
        db.rollback()
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
        )
        db.add(note)
        encounter.pipeline_status = EncounterPipelineStatus.NOTE_GENERATED
        db.add(encounter)
        db.commit()
        db.refresh(note)
        return note.id
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise self.retry(exc=exc, countdown=30) from exc
    finally:
        db.close()
