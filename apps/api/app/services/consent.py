"""Consent-gate enforcement (P0-1, Phase 0.1).

Before this module existed, nothing server-side stopped an encounter
from being uploaded and transcribed with zero consent records — the
rule lived only in the mobile client's UI, which is exactly where a
legal control must not live, since the client is the part a bug or an
attacker controls. This is the single place that answers "is it
currently legal to touch this encounter's audio?", so every enforcement
point (upload confirmation, the transcription task, anything added
later) shares one definition of "valid consent" instead of each
re-deriving it against the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.consent import ConsentLedgerEntry


class ConsentNotValidError(Exception):
    """Raised when an encounter has no currently-active consent.

    This is a *state* problem, not a *permissions* problem — the caller
    is allowed to be here, the ledger just doesn't support it yet (or
    any more). Route handlers should map this to 409, not 403.
    """


@dataclass(frozen=True)
class ConsentState:
    """The ledger folded into an answer. Phase 2.2 needs this as a *value*
    rather than only as an exception: P0-1 says the app must block
    recording when no consent exists, and to block it the client has to be
    able to ask. Local client state cannot answer it — a page reload mid-
    encounter loses that, while the ledger entry persists — so the read
    has to come from here.
    """

    encounter_id: str
    is_given: bool
    latest_event: str | None
    script_language: str | None
    entry_count: int


def current_consent_state(db: Session, encounter_id: str) -> ConsentState:
    """The single fold over the ledger. `assert_consent_valid` and the
    read endpoint both go through this, so "valid consent" keeps exactly
    one definition — the property this module's docstring promises.

    The ledger is append-only (enforced by a DB trigger — see
    alembic/versions/..._append_only_consent_ledger.py), so "current
    state" is never a column to read; it's always a fold over history.
    A "declined" or "withdrawn" event after a "given" one revokes it; a
    later "given" event (re-consent) restores it. No rows at all fails
    closed, same as a "declined" row would.
    """
    entries = (
        db.query(ConsentLedgerEntry)
        .filter(ConsentLedgerEntry.encounter_id == encounter_id)
        .order_by(ConsentLedgerEntry.created_at.asc())
        .all()
    )

    is_given = False
    for entry in entries:
        if entry.event == "given":
            is_given = True
        elif entry.event in ("declined", "withdrawn"):
            is_given = False

    latest = entries[-1] if entries else None
    return ConsentState(
        encounter_id=encounter_id,
        is_given=is_given,
        latest_event=latest.event if latest else None,
        script_language=latest.script_language if latest else None,
        entry_count=len(entries),
    )


def assert_consent_valid(db: Session, encounter_id: str) -> None:
    """Raise ConsentNotValidError unless the ledger currently implies
    "given". Thin wrapper over `current_consent_state` on purpose: the
    enforcement points (upload confirmation, the transcription task) and
    the client-facing read must never be able to disagree.
    """
    if not current_consent_state(db, encounter_id).is_given:
        raise ConsentNotValidError(f"Encounter {encounter_id} has no active consent record.")


@dataclass(frozen=True)
class WithdrawalOutcome:
    """What actually happened when a withdrawal was processed. Returned
    rather than logged-and-forgotten because the API tells the client, and
    the client has to tell the doctor whether the audio is gone — "we think
    it's deleted" is not something to guess about in front of a patient.
    """

    encounter_id: str
    #: The pipeline stops at the next stage boundary, which Phase 0.1 already
    #: enforces. Never "instantly": you cannot reliably kill a Celery task
    #: mid-flight, and telling Legal otherwise would be a lie.
    pipeline_will_stop: bool
    #: True once the uploaded object is confirmed gone from object storage.
    audio_deleted: bool
    #: True when there was no uploaded audio to delete in the first place.
    nothing_to_delete: bool
    #: Set to now on withdrawal so any retention sweep collects it
    #: immediately rather than at the end of the normal 90-day clock.
    retention_expired_immediately: bool


def handle_withdrawal(db: Session, encounter_id: str) -> WithdrawalOutcome:
    """The server-side half of P0-1's withdrawal requirement: "processing
    stops and the associated audio is queued for deletion without undue
    delay."

    Three things happen, in an order chosen so a failure in the least
    important one cannot undo the most important:

    1. **The ledger entry is already committed** by the caller before this
       runs. That entry is the legal record; it must survive even if
       everything below fails.
    2. **The retention clock is set to now.** This is the durable backstop —
       whatever happens to the delete call below, the encounter is now
       eligible for collection by the retention job (Phase 4.4) instead of
       sitting for 90 days.
    3. **An immediate delete is attempted.** Best-effort by design: a
       withdrawal must not fail because object storage was briefly
       unreachable.

    Deliberately does NOT try to abort a running Celery task. The
    checklist's own heads-up is explicit that you cannot reliably kill a
    task mid-flight, so the design is "stops at the next checkpoint" —
    which Phase 0.1 already guarantees by re-checking consent at the head of
    `transcribe_encounter` and at upload confirmation. `pipeline_will_stop`
    reports that honestly rather than implying instant abort.

    Idempotent: withdrawing twice is not an error, and the second call
    reports `nothing_to_delete` once the object is gone.
    """
    from datetime import datetime, timezone

    from app.models.encounter import Encounter
    from app.services import storage

    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        return WithdrawalOutcome(
            encounter_id=encounter_id,
            pipeline_will_stop=False,
            audio_deleted=False,
            nothing_to_delete=True,
            retention_expired_immediately=False,
        )

    now = datetime.now(timezone.utc)
    encounter.audio_retention_expires_at = now

    key = encounter.audio_object_key
    already_gone = encounter.audio_deleted_at is not None
    deleted = False

    if key and not already_gone:
        deleted = storage.delete_object(key)
        if deleted:
            encounter.audio_deleted_at = now

    db.add(encounter)
    db.commit()

    return WithdrawalOutcome(
        encounter_id=encounter_id,
        pipeline_will_stop=True,
        audio_deleted=deleted or already_gone,
        nothing_to_delete=key is None,
        retention_expired_immediately=True,
    )
