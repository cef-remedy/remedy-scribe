"""Phase 4.2 (P0-8) review-interface schemas.

Extends `app/schemas/audit_log.py`'s `AuditLogOut` rather than replacing
it: the list endpoint's element shape stays backwards-compatible (0.2
already shipped it and there is a test asserting it), and the two fields a
reviewer needs on top — the non-PHI `diff` and the row's retention date —
are added here.
"""

from datetime import datetime

from pydantic import BaseModel

from app.schemas.audit_log import AuditLogOut


class AuditLogEntryOut(AuditLogOut):
    """One audit row as the review interface renders it.

    `diff` is safe to expose because nothing may put PHI in it — see
    `app/models/audit_log.py`'s class docstring. `retention_expires_at` is
    exposed because a reviewer looking at a nearly-expired row needs to
    know it is about to become unavailable, and because it is the value the
    append-only trigger keys off: before this date the row cannot be
    deleted by anyone, including the app's own DB role.
    """

    diff: str | None
    retention_expires_at: datetime


class AuditAccessReportRow(BaseModel):
    """One (actor, action) pair in the access report for a single record.

    Grouped rather than listed because the question P0-8 exists to answer —
    "who looked at this patient's record?" — is answered by a handful of
    names, not by five hundred individual rows. The raw rows are still one
    `GET /audit-logs?entity_id=...` away.

    The actor's name/email/role are joined in here (they are staff
    identity, not PHI) because an audit report naming only UUIDs is one
    nobody can review, which is the failure mode "reviewable" is guarding
    against. They are read live from `clinicians`, never copied into
    `audit_logs`, so a renamed or deactivated account reports its current
    identity and a deleted one degrades to nulls rather than to a stale
    name frozen years ago.
    """

    actor_clinician_id: str | None
    actor_email: str | None
    actor_full_name: str | None
    actor_role: str | None
    action: str
    access_count: int
    first_at: datetime
    last_at: datetime
