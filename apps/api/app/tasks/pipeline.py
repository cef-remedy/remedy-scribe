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

Phase 5.2 adds the third thing neither of those does: **saying so.**
`monitor_pipeline_health` below is the job that watches the two watchers,
and every task in this module now carries a correlation ID and emits its
own latency, so "note generation is sometimes slow" can be turned into a
question about a specific encounter's audio length.

**Correlation across the async boundary.** A `ContextVar` does not survive
serialisation into Redis, so the ID is passed as an explicit task kwarg —
that is the whole of 5.2's 📚. It is only ever *read* from the ambient
context, never relied upon to cross a process: `run_pipeline` picks up the
ID the HTTP middleware bound (or the sweep bound) and writes it into both
task signatures, so a browser request, a transcription and a note
generation minutes later all carry the same ID with no route changes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core import metrics
from app.core.config import get_settings
from app.core.observability import (
    correlation_scope,
    current_correlation_id,
    log_event,
    new_correlation_id,
    register_sensitive,
    safe_exception_summary,
    sensitive_scope,
    stage_timer,
)
from app.db.session import SessionLocal
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note
from app.services.asr import get_asr_provider
from app.services.consent import ConsentNotValidError, assert_consent_valid
from app.services.note_generation import get_note_generator
from app.services.transcripts import load_transcript, persist_transcript
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _sampler(series: str, encounter_id: str):
    """A `stage_timer` on_finish hook that records this stage's latency.

    Only successful runs are sampled. A failed attempt's duration is a real
    and useful number — it is in the emitted log line — but mixing it into
    the latency percentiles would make a vendor outage look like a
    performance improvement (a stage that fails in 40 ms drags p50 down),
    and latency percentiles are read to answer "is this slow for doctors?",
    a question only completed work can answer.
    """

    def record(duration_ms: int, status: str) -> None:
        if status == "ok":
            metrics.record_sample(series, encounter_id, float(duration_ms))

    return record


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_pipeline(encounter_id: str, *, correlation_id: str | None = None) -> None:
    """Entry point called by the upload-confirmation route. Kept as a
    plain function (rather than a single mega-task) so it's easy to call
    synchronously in tests without a running Celery worker/broker.

    `correlation_id` defaults to whatever is bound in the calling context —
    the HTTP request's ID inside a route, the sweep run's ID inside
    `sweep_stuck_encounters`. Keeping it a keyword with a default is
    deliberate: the two call sites live in files this phase does not own
    (`routes/uploads.py`, `routes/encounters.py`) and they keep working
    unchanged, which is the difference between threading a correlation ID
    and rewriting the call graph to thread one.
    """
    resolved = correlation_id or current_correlation_id() or new_correlation_id("pipeline")
    # Explicit on both signatures. `generate_note` receives `encounter_id`
    # positionally from the chain (transcribe returns it), so the kwarg is
    # all that has to be pinned here.
    chain = transcribe_encounter.s(encounter_id, correlation_id=resolved) | generate_note.s(correlation_id=resolved)
    chain.apply_async()


def run_note_generation(encounter_id: str, *, correlation_id: str | None = None) -> None:
    """Re-run just `generate_note` — used by `/encounters/{id}/retry` when
    an encounter is `GENERATION_FAILED`. The transcript already exists
    (transcription succeeded); re-running the whole chain would re-pay
    for a real ASR call the first attempt already got right, for no
    reason. Also used by `sweep_stuck_encounters` for the same reason
    when a `TRANSCRIBED` encounter is found stuck.
    """
    resolved = correlation_id or current_correlation_id() or new_correlation_id("pipeline")
    generate_note.apply_async(args=[encounter_id], kwargs={"correlation_id": resolved})


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
    # Phase 5.2. Was `str(exc)[:500]`, whose safety rested entirely on the
    # column comment's claim that "every exception raised from these two
    # tasks is an infrastructure/vendor error ... never something built
    # from transcript or note content." Phase 4.0 found that claim already
    # false on one path (`_extract_tool_input` interpolated the whole model
    # response into its message) and wrote generated clinical prose into
    # this unencrypted column. Fixing the one exception fixed the one
    # exception; routing the *write* through the scrubber fixes the class,
    # so the next author of an over-detailed exception message cannot
    # reintroduce it. Truncation was never a control either — the first 500
    # characters of a note are still a note.
    encounter.last_pipeline_error = safe_exception_summary(exc)
    encounter.pipeline_updated_at = _utcnow()
    if exhausted:
        encounter.pipeline_status = failed_status
    db.add(encounter)
    db.commit()
    log_event(
        logger,
        "pipeline.stage.failed",
        level=logging.ERROR if exhausted else logging.WARNING,
        exc_info=True,
        encounter_id=encounter_id,
        stage=failed_status.value.removesuffix("_failed"),
        attempt=encounter.retry_count,
        max_attempts=self.max_retries,
        terminal=exhausted,
        error_type=type(exc).__name__,
    )
    return exhausted


@celery_app.task(name="pipeline.transcribe_encounter", bind=True, max_retries=3)
def transcribe_encounter(self, encounter_id: str, correlation_id: str | None = None) -> str:
    """Phase 5.2 wraps the body in three context managers and changes
    nothing else about it:

    * `correlation_scope` — binds the ID the request (or the sweep) minted,
      so every line this task and everything it calls emits carries it.
    * `sensitive_scope` — opened **outside** the `try`, deliberately, so the
      `except` blocks are inside it. That is where `_mark_stage_failure`
      writes to an unencrypted column, and it is the exact spot Phase 4.0's
      leak would have surfaced.
    * `stage_timer` — one latency line per attempt, including the failed
      attempts, which is what separates "the vendor is slow" from "the API
      key is missing".
    """
    with (
        correlation_scope(correlation_id, origin="pipeline"),
        sensitive_scope(),
        stage_timer(
            logger,
            "pipeline.stage.transcribe",
            stage="transcribe",
            encounter_id=encounter_id,
            on_finish=_sampler(metrics.SERIES_TRANSCRIBE_MS, encounter_id),
        ) as timing,
    ):
        db = SessionLocal()
        try:
            encounter = db.get(Encounter, encounter_id)
            if encounter is None:
                raise ValueError(f"Encounter {encounter_id} not found")
            if encounter.pipeline_status in (
                EncounterPipelineStatus.TRANSCRIBED,
                EncounterPipelineStatus.NOTE_GENERATED,
            ):
                timing["status"] = "noop"
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
            # Registered the moment it exists and before anything can raise
            # while holding it: from here on, a transcript segment reaching a
            # log line or an exception message is replaced rather than
            # emitted.
            register_sensitive(*(segment.text for segment in segments))
            persist_transcript(
                db,
                encounter_id,
                provider_name=provider.provider_name,
                model_version=provider.model_version,
                segments=segments,
            )

            encounter.pipeline_status = EncounterPipelineStatus.TRANSCRIBED
            encounter.pipeline_updated_at = _utcnow()
            encounter.retry_count = 0  # this stage succeeded — any prior attempts on it no longer matter
            encounter.last_pipeline_error = None
            db.add(encounter)
            db.commit()
            timing["provider"] = provider.provider_name
            timing["model"] = provider.model_version
            timing["segments"] = len(segments)
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
            # Not "failed". A withdrawal honoured is the system working, and
            # counting it as a stage failure would fire the failure-rate
            # alert on P0-1 doing its job.
            timing["status"] = "blocked_no_consent"
            return encounter_id
        except Exception as exc:  # noqa: BLE001 - retry any transient provider failure
            if _mark_stage_failure(db, encounter_id, self, exc, EncounterPipelineStatus.TRANSCRIPTION_FAILED):
                raise  # retries exhausted — dead-lettered above; nothing left to retry
            raise self.retry(exc=exc, countdown=30) from exc
        finally:
            db.close()


@celery_app.task(name="pipeline.generate_note", bind=True, max_retries=3)
def generate_note(self, encounter_id: str, correlation_id: str | None = None) -> str:
    """Same three wrappers as `transcribe_encounter`, plus the per-consult
    cost estimate — this is the stage that knows both of the numbers the
    estimate needs (audio length, from the transcript's own word timings,
    and text volume), and it knows them without a single extra query or a
    byte of PHI leaving the function.
    """
    with (
        correlation_scope(correlation_id, origin="pipeline"),
        sensitive_scope(),
        stage_timer(
            logger,
            "pipeline.stage.generate",
            stage="generate",
            encounter_id=encounter_id,
            on_finish=_sampler(metrics.SERIES_GENERATE_MS, encounter_id),
        ) as timing,
    ):
        db = SessionLocal()
        try:
            encounter = db.get(Encounter, encounter_id)
            if encounter is None:
                raise ValueError(f"Encounter {encounter_id} not found")

            existing = db.query(Note).filter(Note.encounter_id == encounter_id).one_or_none()
            if existing is not None:
                timing["status"] = "noop"
                return existing.id  # already generated — redelivered message, no-op

            generator = get_note_generator()
            transcript = load_transcript(db, encounter_id)
            register_sensitive(*(segment.text for segment in transcript))
            generated = generator.generate(transcript=transcript)
            # The generated note is PHI too, and it is the specific PHI that
            # nearly reached an unencrypted column in Phase 4.0 — a model
            # response missing its tool block usually contains prose *about
            # the consultation*.
            register_sensitive(
                generated.assessment.text,
                generated.plan.text,
                generated.subjective.text,
                generated.objective.text,
            )

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

            _record_consult_cost(encounter_id, transcript, generated, generator)
            timing["provider"] = generated.provider
            timing["prompt_version"] = generated.prompt_version
            timing["note_id"] = note.id
            return note.id
        except Exception as exc:  # noqa: BLE001
            if _mark_stage_failure(db, encounter_id, self, exc, EncounterPipelineStatus.GENERATION_FAILED):
                raise
            raise self.retry(exc=exc, countdown=30) from exc
        finally:
            db.close()


def _record_consult_cost(encounter_id: str, transcript, generated, generator) -> None:
    """Estimate and record what this consultation cost, in the one place
    that can do it for free.

    Audio duration comes from the transcript's own last word timestamp
    rather than from a stored duration, because there is no stored duration
    (`Encounter` has no such column) and the transcript is already decrypted
    in memory here. Nothing but counts leaves this function.

    Never raises. A failure to *measure* a note must not fail the note —
    the pipeline's job is the clinical record and this is bookkeeping.
    """
    try:
        audio_ms = max(
            (word.end_ms for segment in transcript for word in segment.words),
            default=0,
        )
        transcript_chars = sum(len(segment.text) for segment in transcript)
        note_chars = sum(
            len(section.text)
            for section in (generated.assessment, generated.plan, generated.subjective, generated.objective)
        )
        cost = metrics.estimate_consult_cost(
            audio_seconds=audio_ms / 1000.0,
            transcript_chars=transcript_chars,
            note_chars=note_chars,
        )
        metrics.record_sample(metrics.SERIES_CONSULT_USD, encounter_id, cost.total_usd)
        log_event(
            logger,
            "cost.consult.estimated",
            encounter_id=encounter_id,
            provider=generated.provider,
            model=getattr(generator, "model_version", None),
            audio_seconds=round(cost.audio_seconds, 1),
            chars=transcript_chars,
            tokens_in=cost.tokens_in,
            tokens_out=cost.tokens_out,
            usd=round(cost.total_usd, 5),
        )
    except Exception:  # noqa: BLE001 - measurement must never break the measured
        logger.debug("Could not estimate consult cost", exc_info=True)


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

    **What a correlation ID means here (Phase 5.2).** There is no inbound
    request, so there is nothing to inherit — and leaving the field blank
    would make the sweep the one part of the pipeline you cannot trace. So
    the sweep mints its *own* ID for the run (`sweep-...`) and every
    encounter it re-kicks inherits it. That answers a question the
    request-scoped ID cannot: "which sweep run rescued this encounter, and
    what else did that same run touch?" — a burst of failures sharing one
    `sweep-` ID is a broker or worker problem; the same failures spread
    across many IDs is a per-encounter problem. The trade is deliberate and
    worth naming: a re-kicked encounter's new trace does **not** share an ID
    with its original upload request, because a run identifier and a
    request identifier are different things and merging them would let one
    sweep of 200 encounters adopt 200 unrelated request IDs.
    """
    with correlation_scope(None, origin="sweep-stuck"):
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
            log_event(
                logger,
                "sweep.stuck_encounters.finished",
                level=logging.WARNING if stuck else logging.INFO,
                count=len(stuck),
            )
            # Recorded last, and only on a clean run: the heartbeat's whole
            # purpose is to let `monitor_pipeline_health` alert when this
            # job stops working, and stamping it before the work would make
            # a job that crashes every time look perfectly healthy.
            metrics.record_heartbeat(metrics.HEARTBEAT_STUCK_SWEEP)
            return len(stuck)
        finally:
            db.close()


@celery_app.task(name="pipeline.monitor_pipeline_health")
def monitor_pipeline_health() -> int:
    """The job that watches the jobs (Phase 5.2, P0-8).

    Phase 4 closed with a gap stated in as many words: the retention sweep
    and the stuck-encounter sweep are the code that exists *because* nothing
    was watching, and nothing was watching them either. Both run inside a
    single `celery beat` process; if that process dies, the app keeps
    serving, notes keep generating, and the only symptoms are that stuck
    encounters stop being rescued and expired PHI stops being deleted. Both
    are silent, both are cumulative, and the second one is a Data Privacy
    Act problem accruing at one consultation per patient.

    So this task reads one snapshot, emits every number as a metric event,
    evaluates the alert rules and emits the breaches — with `critical` at
    ERROR, which is the level Sentry turns into an issue. **That mapping is
    the entire delivery mechanism**, and it only reaches a human when
    SENTRY_DSN is set. Without one these are lines in a log file. See
    docs/runbooks/observability.md; nothing here pretends otherwise.

    Returns the number of firing alerts so a manual `.apply()` from a shell
    is useful on its own.
    """
    with correlation_scope(None, origin="monitor"):
        db = SessionLocal()
        try:
            snapshot = metrics.collect_snapshot(db)
            metrics.emit_snapshot(snapshot)
            alerts = metrics.evaluate_alerts(snapshot)
            metrics.emit_alerts(alerts)
            metrics.record_heartbeat(metrics.HEARTBEAT_MONITOR)
            return len(alerts)
        finally:
            db.close()
