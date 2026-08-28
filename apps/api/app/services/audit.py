"""The audit trail's single write path (P0-8).

**Action vocabulary.** `action` is a dotted string, `<entity>.<verb>`,
with a `.read` segment wherever the event is a *disclosure* rather than a
change. That distinction is the whole point of Phase 4.2: an access log
that only records writes answers "who changed this?" and cannot answer
"who looked at this?", which is the question a breach investigation
actually asks. As of 4.2 the vocabulary is:

    patient.match                     patient.search
    patient.create
    note.read                         note.read.prior_visit
    note.grounding.read               note.edit
    note.transition.<status>
    encounter.create                  encounter.resume
    encounter.read                    encounter.list.loose
    encounter.list.failed             encounter.link_patient
    encounter.retry                   encounter.audio.playback_url
    encounter.upload.init             encounter.upload.part_url
    encounter.upload.complete
    consent.read                      consent.record.<event>
    audit_log.read                    audit_log.access_report

Reads are recorded *after* the data has been successfully assembled, not
before: a 404 or a 409 disclosed nothing, and logging an access that
never happened makes the trail harder to read, not safer.
"""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog

# Phase 4.2: the coalescing window used by the two endpoints a client
# calls in a loop rather than once per human action — the upload queue's
# `GET /encounters/{id}` poll and the per-part presigned-URL mint. See
# `record`'s `coalesce_seconds` below and docs/decisions/0032.
POLL_COALESCE_SECONDS = 60


def record(
    db: Session,
    *,
    actor_clinician_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    diff: dict | None = None,
    coalesce_seconds: int | None = None,
) -> None:
    """The single write path for app_logs (P0-8). Called explicitly from
    routes that read/change sensitive entities (patients, notes, consent) —
    explicit call sites, rather than a generic request-logging middleware,
    so `action`/`entity_type` stay meaningful (e.g. "note.sign") instead of
    a raw HTTP method + path.

    Never pass PHI in `diff`. See app/models/audit_log.py's class docstring:
    rows here outlive what they describe and, since Phase 4.2, cannot be
    edited or deleted to take anything back out.

    `coalesce_seconds` (Phase 4.2, opt-in per call site): when an identical
    (actor, action, entity) event was already recorded within that many
    seconds, this is a no-op. It exists for the machine-driven endpoints —
    the upload queue polls `GET /encounters/{id}` every few seconds until
    the pipeline confirms, which would otherwise write hundreds of
    indistinguishable rows per recording and bury the human accesses they
    sit between. The first access is *always* recorded, and a continuing
    one is re-recorded every window, so "did this clinician access this
    record, when, and for how long" survives; only the exact hit count is
    lost. Default `None` means never coalesce — every write path and every
    human-initiated read keeps full fidelity, which is why this is opt-in
    rather than a global setting.
    """
    if coalesce_seconds is not None and _recorded_recently(
        db,
        actor_clinician_id=actor_clinician_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        within_seconds=coalesce_seconds,
    ):
        return

    db.add(
        AuditLog(
            actor_clinician_id=actor_clinician_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            diff=json.dumps(diff) if diff is not None else None,
        )
    )
    db.commit()


def _recorded_recently(
    db: Session,
    *,
    actor_clinician_id: str | None,
    action: str,
    entity_type: str,
    entity_id: str,
    within_seconds: int,
) -> bool:
    """Has this exact (actor, action, entity) triple been recorded inside
    the window? Deliberately a plain SELECT with no locking: two concurrent
    polls racing here write two rows instead of one, which is a harmless
    outcome — the failure mode worth avoiding is a *missing* row, and this
    cannot cause one.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
    return (
        db.query(AuditLog.id)
        .filter(
            AuditLog.actor_clinician_id == actor_clinician_id,
            AuditLog.action == action,
            AuditLog.entity_type == entity_type,
            AuditLog.entity_id == entity_id,
            AuditLog.created_at >= cutoff,
        )
        .first()
        is not None
    )
