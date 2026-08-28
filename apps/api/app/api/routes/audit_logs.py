"""The audit-log review interface (P0-8: "access and change logs retained
and **reviewable**").

0.2 shipped a stub here — an unfiltered, unpaginated list — deliberately
early, so the RBAC boundary it guards had something real to test
(docs/decisions/0005). Phase 4.2 makes it the thing the requirement
actually asks for. "Reviewable" is not "the rows exist"; it is a
compliance officer being able to sit down and answer, in one query:

    "Who looked at this patient's record, and when?"

That is `GET /audit-logs/access-report?entity_type=patient&entity_id=...`.
Everything else here supports it: the filtered list is the drill-down from
a report row to the individual accesses behind it.

Two properties worth stating explicitly:

- **Reading the audit log is itself audited.** A review interface that
  leaves no trace makes the audit trail the one PHI-adjacent surface in
  the system nobody is accountable for reading, and access logs are read
  precisely when someone is under suspicion. The audit row is written
  *after* the query succeeds, so a 403 or a failed query does not log a
  disclosure that never happened.
- **Still `compliance`/`admin` only**, including the doctor whose own
  actions are in the log. Unchanged from 0.2; the tests in
  tests/test_rbac.py cover it.

The list response stays a bare JSON array (not a `{items, total}`
envelope) so the shape 0.2 published — and the client generated from it —
does not break. Pagination metadata rides in `X-Total-Count` /
`X-Limit` / `X-Offset` headers instead.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.audit_log import AuditLog
from app.models.clinician import Clinician
from app.schemas.audit import AuditAccessReportRow, AuditLogEntryOut
from app.services import audit

router = APIRouter(prefix="/audit-logs", tags=["audit"])

# A cap, not a preference: an unbounded list endpoint over the largest and
# fastest-growing table in the system is a denial-of-service waiting to be
# discovered by an honest reviewer clicking "show all".
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100


@router.get("", response_model=list[AuditLogEntryOut])
def list_audit_logs(
    response: Response,
    actor_clinician_id: str | None = Query(None, description="Everything this clinician did"),
    entity_type: str | None = Query(None, description='e.g. "patient", "note", "encounter"'),
    entity_id: str | None = Query(None, description="Everything done to this one record"),
    action: str | None = Query(None, description="Exact action, e.g. note.grounding.read"),
    action_prefix: str | None = Query(None, description='Dotted prefix, e.g. "note." or "encounter.upload."'),
    since: datetime | None = Query(None, description="Inclusive lower bound on created_at"),
    until: datetime | None = Query(None, description="Exclusive upper bound on created_at"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("compliance", "admin")),
) -> list[AuditLogEntryOut]:
    """The drill-down: every matching audit row, newest first.

    `action_prefix` exists because the vocabulary is deliberately dotted
    (see app/services/audit.py) — "show me everything anyone did to notes"
    is `note.`, and "every disclosure of audio" is
    `encounter.audio.`. Without it a reviewer has to know the exact action
    strings before they can look for anything.
    """
    query = db.query(AuditLog)
    if actor_clinician_id is not None:
        query = query.filter(AuditLog.actor_clinician_id == actor_clinician_id)
    if entity_type is not None:
        query = query.filter(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        query = query.filter(AuditLog.entity_id == entity_id)
    if action is not None:
        query = query.filter(AuditLog.action == action)
    if action_prefix is not None:
        # autoescape: a reviewer typing "note.%" means those characters,
        # not a wildcard. Without it, "%" quietly matches everything and
        # the report looks complete when it is not.
        query = query.filter(AuditLog.action.startswith(action_prefix, autoescape=True))
    if since is not None:
        query = query.filter(AuditLog.created_at >= since)
    if until is not None:
        query = query.filter(AuditLog.created_at < until)

    total = query.count()
    rows = (
        # Tie-broken by id, not ordered by created_at alone: decision 0027
        # already recorded that rows written in the same request can share a
        # created_at to the microsecond, and an unstable sort makes paging
        # through a report silently skip and repeat rows.
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset).all()
    )

    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    _record_review(
        db,
        clinician=clinician,
        action="audit_log.read",
        entity_type=entity_type,
        entity_id=entity_id,
        filters={
            "actor_clinician_id": actor_clinician_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "action": action,
            "action_prefix": action_prefix,
            "since": since,
            "until": until,
        },
    )
    return [AuditLogEntryOut.model_validate(r) for r in rows]


@router.get("/access-report", response_model=list[AuditAccessReportRow])
def access_report(
    entity_type: str = Query(description='The record\'s type, e.g. "patient", "note", "encounter"'),
    entity_id: str = Query(description="The record's id"),
    since: datetime | None = Query(None, description="Inclusive lower bound on created_at"),
    until: datetime | None = Query(None, description="Exclusive upper bound on created_at"),
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("compliance", "admin")),
) -> list[AuditAccessReportRow]:
    """**"Who looked at this record, and when?"** — the question P0-8 exists
    to make answerable, in one call.

    One row per (actor, action), ordered by most recent activity, with the
    actor's real name resolved so the report reads as names rather than
    UUIDs. `LEFT OUTER JOIN` on the clinician: `actor_clinician_id` is
    nullable (a system-initiated action has no human actor) and the row
    must still appear — an unattributed access is the *most* interesting
    line in a breach investigation, and an inner join would silently drop
    exactly those.

    Note that this only reports what the trail contains. A record with no
    rows returns an empty report, which means "no logged access", not "no
    access" — the two are the same claim only because every disclosure path
    in the API writes here (Phase 4.2's actual work).
    """
    query = (
        db.query(
            AuditLog.actor_clinician_id.label("actor_clinician_id"),
            Clinician.email.label("actor_email"),
            Clinician.full_name.label("actor_full_name"),
            Clinician.role.label("actor_role"),
            AuditLog.action.label("action"),
            func.count(AuditLog.id).label("access_count"),
            func.min(AuditLog.created_at).label("first_at"),
            func.max(AuditLog.created_at).label("last_at"),
        )
        .outerjoin(Clinician, Clinician.id == AuditLog.actor_clinician_id)
        .filter(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
    )
    if since is not None:
        query = query.filter(AuditLog.created_at >= since)
    if until is not None:
        query = query.filter(AuditLog.created_at < until)

    rows = query.group_by(
        AuditLog.actor_clinician_id,
        Clinician.email,
        Clinician.full_name,
        Clinician.role,
        AuditLog.action,
    ).all()
    # Sorted in Python, not SQL: ordering by an aggregate alias is the one
    # bit of this query whose spelling differs between SQLite (tests) and
    # Postgres (deploy), and there are at most a handful of actors per
    # record. See docs/decisions/0032.
    rows = sorted(rows, key=lambda r: r.last_at, reverse=True)

    report = [
        AuditAccessReportRow(
            actor_clinician_id=r.actor_clinician_id,
            actor_email=r.actor_email,
            actor_full_name=r.actor_full_name,
            actor_role=r.actor_role,
            action=r.action,
            access_count=r.access_count,
            first_at=r.first_at,
            last_at=r.last_at,
        )
        for r in rows
    ]

    # Recorded against the *reviewed* record, not against "audit_log" in
    # the abstract, so that pulling a patient's access history shows up in
    # that patient's own history the next time someone pulls it.
    _record_review(
        db,
        clinician=clinician,
        action="audit_log.access_report",
        entity_type=entity_type,
        entity_id=entity_id,
        filters={"since": since, "until": until},
    )
    return report


def _record_review(
    db: Session,
    *,
    clinician: Clinician,
    action: str,
    entity_type: str | None,
    entity_id: str | None,
    filters: dict[str, object | None],
) -> None:
    """Audits a review of the audit log.

    Only the *names* of the filters used are recorded, never their free-text
    values — with the deliberate exception of `entity_type`/`entity_id`,
    which are surrogate keys this table already stores by the million and
    which are the only part of a review worth being accountable for ("she
    pulled this patient's history"). This is the same rule
    `patients.search` follows for its query string: a search term is
    user-typed text, it can contain a patient name, and this table keeps
    what it is given for years.
    """
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action=action,
        entity_type=entity_type or "audit_log",
        entity_id=entity_id or "*",
        diff={"filters": sorted(k for k, v in filters.items() if v is not None)},
    )
