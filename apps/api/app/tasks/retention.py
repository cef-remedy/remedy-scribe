"""Retention enforcement (Phase 4.4) — the job that finally *reads* the
two retention clocks the schema has been writing since Phase 1.1 and 1.2.

Until this module existed, `Encounter.audio_retention_expires_at` and
`Transcript.retention_expires_at` were set on every row and read by
nothing: retention was a column, not a policy. The Compliance story
("audio retention duration is a configurable value") was half true — the
value was configurable, but nothing acted on it.

**Two enforcement layers, deliberately, not one** (decision 0033):

* The bucket lifecycle rule (`storage.ensure_bucket_configured`) expires
  the audio objects themselves. It runs inside object storage, so it
  holds whether or not this application is running, deployed, or even
  correct. That is the backstop, and it is the one that matters most
  because the raw recording is the most sensitive artifact.
* This job handles everything the bucket cannot see: the Postgres rows
  that are *derived* from that audio — the transcript and the note's
  drafting history — plus the write-back that keeps the encounter row
  honest about what has already gone.

Neither replaces the other. A lifecycle rule cannot reach Postgres; an
application job cannot be trusted to have run.

**What is never deleted.** A signed `Note` is a permanent medical record;
nothing here touches the `notes` table, in any code path, for any reason.
`NoteRevision` rows are the *drafting history* — the raw material for the
Phase 6 edit-burden metric, not the record itself — and those do expire.
The consent ledger is likewise never touched (it is append-only at the DB
level anyway, and it is the legal record of the withdrawal that may have
triggered the deletion in the first place).

**Why revisions and the transcript die together.** `grounding.py` derives
`edited_since_generation` from a `NoteRevision` merely *existing* for a
section. Deleting revisions while the transcript survives would therefore
flip a doctor-rewritten section back to "these are the model's words" and
let the grounding UI highlight transcript passages as the source of prose
the model never wrote — exactly the confidently-wrong answer decision 0030
exists to prevent. So revisions are only ever removed in the same purge
that removes (or has already removed) the transcript, at which point
grounding reports `TranscriptState.EXPIRED`, returns no segments, and has
nothing left to mis-attribute.

**Why deleting audio here keeps grounding's five-state ladder honest.**
`_audio_state` distinguishes `WITHDRAWN` from `EXPIRED` by asking the
consent ledger, not by anything this job writes. Stamping
`audio_deleted_at` (which this job does, and must — otherwise every later
read pays a pointless `HEAD` against a key that will never come back) is
therefore reason-neutral: a withdrawal still reads as a withdrawal because
its ledger entry still exists, and a time-expiry still reads as an expiry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.observability import correlation_scope, log_event
from app.db.session import SessionLocal
from app.models.consent import ConsentEventType, ConsentLedgerEntry
from app.models.encounter import Encounter
from app.models.note import Note, NoteRevision
from app.models.transcript import Transcript
from app.services import audit, storage
from app.services.consent import (
    WithdrawalOutcome,
    current_consent_state,
    handle_withdrawal,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

#: Most work one sweep will take on. Deletion is destructive and talks to
#: object storage one key at a time, so an unbounded run against a
#: long-neglected database is the wrong shape: it would hold a
#: transaction open for however long S3 takes to answer N times. The
#: sweep is re-entrant by construction (everything it purges stops
#: matching its own due-query), so a backlog simply drains over the
#: following hours instead of in one very long run. Not a setting yet —
#: it is an operational safety valve, not a policy knob, and the policy
#: knob that *does* exist (`audio_retention_days`) is the configurable
#: value Compliance actually owns.
_SWEEP_BATCH_LIMIT = 500


class PurgeReason(str, Enum):
    """Why a purge happened, carried into the audit trail (P0-8).

    A string enum rather than a dispatch table keyed by function: Phase
    1.5 learned the hard way (see `sweep_stuck_encounters`' docstring)
    that anything resolving *functions* at import time defeats
    monkeypatching and drags a live broker connection into a unit test.
    Nothing in this module binds a callable at module scope.
    """

    #: The clock in the database ran out. This is the ordinary path.
    RETENTION_EXPIRED = "retention_expired"
    #: P0-1: the patient withdrew consent. Ignores the clocks entirely.
    CONSENT_WITHDRAWN = "consent_withdrawn"


@dataclass(frozen=True)
class PurgeResult:
    """What one encounter's purge actually did. Returned rather than only
    logged for the same reason `WithdrawalOutcome` is: the withdrawal
    endpoint tells a doctor, standing in front of a patient, whether the
    recording is gone. "We think so" is not an acceptable answer.
    """

    encounter_id: str
    audio_deleted: bool
    transcript_deleted: bool
    revisions_deleted: int

    @property
    def anything_deleted(self) -> bool:
        return self.audio_deleted or self.transcript_deleted or self.revisions_deleted > 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(value: datetime | None, now: datetime) -> bool:
    """Has this retention clock run out?

    NULL is never expired — it means "no clock was ever set" (an encounter
    that never got as far as an upload), not "expired long ago". Failing
    open here would delete rows the policy never covered.

    The `tzinfo` normalisation is not defensive noise: these columns are
    `DateTime(timezone=True)`, which Postgres honours and SQLite does not.
    The same row therefore reads back aware on the deployment target and
    naive under the test suite, and comparing a naive datetime to an aware
    one raises `TypeError` rather than returning a wrong answer. Values
    are only ever *written* as UTC, so reading a naive one back as UTC is
    a restatement, not an assumption.
    """
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= now


def _audit_audio(
    db: Session,
    encounter: Encounter,
    reason: PurgeReason,
    actor_clinician_id: str | None,
) -> None:
    """P0-8 for a deletion. The object key is deliberately *not* recorded:
    it is a direct pointer at PHI bytes, and an audit row outlives the
    retention window of the thing it points at — the same reasoning that
    keeps the key out of the `encounter.audio.playback_url` audit entry
    (decision 0030).

    `audit.record` commits, which is exactly why it is called with the
    deletion still pending in the session: the deletion and the record
    of it land in the same commit. An audit
    trail that can be committed without the deletion (or vice versa) is
    an audit trail that can lie.
    """
    audit.record(
        db,
        actor_clinician_id=actor_clinician_id,
        action="encounter.audio.delete",
        entity_type="encounter",
        entity_id=encounter.id,
        diff={"reason": reason.value},
    )


def _delete_revisions(db: Session, encounter_id: str) -> tuple[int, str | None]:
    """Removes the drafting history for an encounter's note. Returns
    (count, note_id) — the note id only so the audit row can point at
    something durable, since the revisions themselves are about to stop
    existing.

    A bulk `DELETE ... WHERE note_id IN (...)` rather than loading rows:
    `previous_text`/`new_text` are `EncryptedString`, and loading them
    would decrypt every historical version of a note into process memory
    purely to throw it away.

    The `notes` row itself is untouched here and everywhere else in this
    module. That boundary is the whole point of the distinction: the note
    is the medical record, the revisions are how it was typed.
    """
    note_id = db.query(Note.id).filter(Note.encounter_id == encounter_id).scalar()
    if note_id is None:
        return 0, None
    count = db.query(NoteRevision).filter(NoteRevision.note_id == note_id).delete(synchronize_session=False)
    return int(count), note_id


def purge_encounter(
    db: Session,
    encounter: Encounter,
    *,
    reason: PurgeReason,
    actor_clinician_id: str | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> PurgeResult:
    """Delete whatever of this encounter's PHI is due, and audit each
    deletion. The single deletion primitive in the system: the periodic
    sweep and the withdrawal path both come through here, so there is one
    definition of "what gets deleted and in what order" rather than two
    that can drift.

    `force=True` ignores both clocks. That is the withdrawal case (P0-1),
    and only the withdrawal case: a patient withdrawing consent does not
    wait 90 days.

    Order matters, and it is the same ordering principle
    `handle_withdrawal` uses — the most important thing first, so a
    failure in a later step cannot undo an earlier one. Audio (the raw
    recording, the most sensitive artifact and the only one living outside
    the database) goes first; the derived rows follow.

    Idempotent: a second call finds `audio_deleted_at` already stamped and
    the rows already gone, deletes nothing, and writes no second audit
    entry. This matters because the sweep runs hourly forever.
    """
    now = now or _utcnow()

    audio_deleted = False
    if (
        encounter.audio_object_key is not None
        and encounter.audio_deleted_at is None
        and (force or _is_expired(encounter.audio_retention_expires_at, now))
    ):
        # Best-effort, like the withdrawal path's delete: storage being
        # briefly unreachable must not stamp `audio_deleted_at` for bytes
        # that are still there. Leaving the row untouched means the next
        # sweep retries it, and until then grounding's `HEAD` check still
        # reports the truth either way.
        if storage.delete_object(encounter.audio_object_key):
            encounter.audio_deleted_at = now
            db.add(encounter)
            audio_deleted = True
            _audit_audio(db, encounter, reason, actor_clinician_id)
        else:
            logger.warning(
                "Retention: could not delete audio for encounter %s; will retry next sweep",
                encounter.id,
            )

    transcript = db.query(Transcript).filter(Transcript.encounter_id == encounter.id).one_or_none()
    transcript_deleted = False
    transcript_id: str | None = None
    if transcript is not None and (force or _is_expired(transcript.retention_expires_at, now)):
        # Read before the delete: the attribute is unreachable once the
        # row is expunged and committed, and the audit row needs it.
        transcript_id = transcript.id
        db.delete(transcript)
        transcript_deleted = True

    # Revisions go only when the transcript is gone — either deleted just
    # now, or already absent on a forced (withdrawal) purge. See the module
    # docstring: dropping them while the transcript survives would make
    # grounding claim a doctor's rewrite as the model's cited words.
    revisions_deleted = 0
    note_id: str | None = None
    if transcript_deleted or (force and transcript is None):
        revisions_deleted, note_id = _delete_revisions(db, encounter.id)

    if transcript_deleted and transcript_id is not None:
        # Commits the transcript delete (and any pending revision delete)
        # together with its audit row — see `_audit_audio`.
        audit.record(
            db,
            actor_clinician_id=actor_clinician_id,
            action="encounter.transcript.delete",
            entity_type="transcript",
            entity_id=transcript_id,
            diff={"reason": reason.value, "encounter_id": encounter.id},
        )
    if revisions_deleted and note_id is not None:
        audit.record(
            db,
            actor_clinician_id=actor_clinician_id,
            action="note.revisions.delete",
            entity_type="note",
            entity_id=note_id,
            # No text, before or after: this records that drafting history
            # was destroyed, not what it said.
            diff={"reason": reason.value, "revisions_deleted": revisions_deleted},
        )

    return PurgeResult(
        encounter_id=encounter.id,
        audio_deleted=audio_deleted,
        transcript_deleted=transcript_deleted,
        revisions_deleted=revisions_deleted,
    )


def purge_withdrawn_encounter(
    db: Session, encounter_id: str, *, actor_clinician_id: str | None = None
) -> WithdrawalOutcome:
    """The immediate-deletion path for P0-1 ("processing stops and the
    associated audio is queued for deletion without undue delay").

    Deliberately *wraps* `handle_withdrawal` rather than reimplementing
    it: that function already owns the parts of a withdrawal that are not
    about deletion — stamping the retention clock to now (the durable
    backstop, so this encounter is collected by the sweep even if
    everything below fails) and returning the `WithdrawalOutcome` the API
    reports to the doctor. What it does not do is reach the *derived* PHI.
    The transcript is arguably more sensitive than the recording it came
    from (verbatim, including what the doctor chose not to write down), so
    a withdrawal that deletes the audio and leaves the transcript has not
    honoured the withdrawal.

    Returns the same `WithdrawalOutcome`, so the consent route can adopt
    this by changing which function it calls and nothing else.
    """
    encounter = db.get(Encounter, encounter_id)
    # Observed *before* the delegation, because `WithdrawalOutcome`
    # reports `audio_deleted=True` for an already-deleted object too — it
    # answers "is it gone", not "did this call remove it", and only the
    # latter is an auditable event.
    audio_present = (
        encounter is not None and encounter.audio_object_key is not None and encounter.audio_deleted_at is None
    )

    outcome = handle_withdrawal(db, encounter_id)
    if encounter is None:
        return outcome

    db.refresh(encounter)
    if audio_present and encounter.audio_deleted_at is not None:
        _audit_audio(db, encounter, PurgeReason.CONSENT_WITHDRAWN, actor_clinician_id)

    purge_encounter(
        db,
        encounter,
        reason=PurgeReason.CONSENT_WITHDRAWN,
        actor_clinician_id=actor_clinician_id,
        force=True,
    )
    return outcome


def _due_encounter_ids(db: Session, now: datetime, limit: int) -> tuple[set[str], set[str]]:
    """(expired_ids, withdrawn_ids) — everything this sweep should look at.

    Two due-queries because there are two genuinely different reasons to
    delete, and they are reported differently in the audit trail. The
    withdrawn set is the backstop half of the withdrawal path: if the
    immediate purge never ran (the route has not adopted it, or storage
    was down when it did), the derived rows are still collected here
    within the hour rather than sitting for 90 days.
    """
    audio_due = {
        row[0]
        for row in db.query(Encounter.id)
        .filter(
            Encounter.audio_object_key.is_not(None),
            Encounter.audio_deleted_at.is_(None),
            Encounter.audio_retention_expires_at.is_not(None),
            Encounter.audio_retention_expires_at <= now,
        )
        .limit(limit)
        .all()
    }
    transcript_due = {
        row[0]
        for row in db.query(Transcript.encounter_id)
        .filter(
            Transcript.retention_expires_at.is_not(None),
            Transcript.retention_expires_at <= now,
        )
        .limit(limit)
        .all()
    }

    # Candidates only: an encounter that was withdrawn *and* still has
    # something derived left to delete. Filtering in SQL keeps this from
    # re-examining every withdrawal in the system's history on every
    # hourly run — once purged, a row stops matching.
    withdrawn_candidates = {
        row[0]
        for row in db.query(Encounter.id)
        .filter(
            Encounter.id.in_(
                select(ConsentLedgerEntry.encounter_id).where(ConsentLedgerEntry.event == ConsentEventType.WITHDRAWN)
            ),
            or_(
                exists().where(Transcript.encounter_id == Encounter.id),
                and_(
                    Encounter.audio_object_key.is_not(None),
                    Encounter.audio_deleted_at.is_(None),
                ),
                exists().where(Note.encounter_id == Encounter.id).where(NoteRevision.note_id == Note.id),
            ),
        )
        .limit(limit)
        .all()
    }
    # A withdrawal can be followed by re-consent — the ledger is a fold,
    # not a latch (see `current_consent_state`). Deleting on the strength
    # of a superseded withdrawal would destroy PHI the patient has since
    # agreed to. Deferring to consent.py's own fold rather than
    # re-deriving it here keeps one definition of "currently consented".
    withdrawn = {eid for eid in withdrawn_candidates if not current_consent_state(db, eid).is_given}

    return (audio_due | transcript_due) - withdrawn, withdrawn


def run_retention_sweep(db: Session, *, now: datetime | None = None, limit: int | None = None) -> dict[str, int]:
    """The sweep body, taking an explicit `Session` so it is callable from
    a test (and, later, a management command) without a broker.

    Calls `purge_encounter` by bare name on purpose. Phase 1.5's
    `sweep_stuck_encounters` documents why at length: a module-level
    structure holding function objects captures them at import time, and a
    test's `monkeypatch.setattr` on the module attribute then silently
    misses every call — which, for a task like this one, means the real
    deletion path runs against real object storage during a unit test.
    """
    now = now or _utcnow()
    limit = _SWEEP_BATCH_LIMIT if limit is None else limit

    expired_ids, withdrawn_ids = _due_encounter_ids(db, now, limit)
    # Sorted for a deterministic, resumable order across runs; withdrawals
    # first because they are the ones a patient is actively waiting on.
    ordered: list[tuple[str, PurgeReason]] = [(eid, PurgeReason.CONSENT_WITHDRAWN) for eid in sorted(withdrawn_ids)] + [
        (eid, PurgeReason.RETENTION_EXPIRED) for eid in sorted(expired_ids)
    ]

    counts = {"encounters": 0, "audio": 0, "transcripts": 0, "revisions": 0}
    for encounter_id, reason in ordered[:limit]:
        encounter = db.get(Encounter, encounter_id)
        if encounter is None:
            continue
        result = purge_encounter(
            db,
            encounter,
            reason=reason,
            force=reason is PurgeReason.CONSENT_WITHDRAWN,
            now=now,
        )
        if result.anything_deleted:
            counts["encounters"] += 1
            counts["audio"] += int(result.audio_deleted)
            counts["transcripts"] += int(result.transcript_deleted)
            counts["revisions"] += result.revisions_deleted

    if counts["encounters"]:
        logger.info("Retention sweep purged %s", counts)
    return counts


@celery_app.task(name="retention.sweep_expired_retention")
def sweep_expired_retention() -> dict[str, int]:
    """Celery Beat runs this hourly (see celery_app.py for why hourly).

    No `actor_clinician_id` on anything it writes: nobody triggered this.
    A NULL actor in `audit_logs` is the honest representation of "the
    retention policy did it", and inventing a service account to blame
    would make the log less true, not more complete.

    **Correlation for a job with no request (Phase 5.2).** Same answer as
    `sweep_stuck_encounters`: one `sweep-retention-...` ID per run, minted
    here, covering every deletion the run performs. That is what makes an
    hour's worth of purges attributable to a single run rather than to
    nothing — and it is the same identifier the audit trail's own reader
    will want when asked "what else did the job that deleted this touch?".
    The audit row itself still records no actor, because a correlation ID
    is an operational trace and an actor is a legal claim about who did it;
    conflating them would put a fabricated actor in the compliance record.

    The heartbeat is the other half. This job's failure mode is silent,
    lawful-looking and cumulative — PHI simply stops being deleted on
    schedule — so "it has not run in N minutes" is the only symptom there
    will ever be, and `monitor_pipeline_health` alerts on it.
    """
    with correlation_scope(None, origin="sweep-retention"):
        db = SessionLocal()
        try:
            counts = run_retention_sweep(db)
            log_event(logger, "sweep.retention.finished", count=counts["encounters"])
            # After the work, never before — see sweep_stuck_encounters.
            metrics.record_heartbeat(metrics.HEARTBEAT_RETENTION_SWEEP)
            return counts
        finally:
            db.close()
