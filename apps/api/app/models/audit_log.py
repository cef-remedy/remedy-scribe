from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

# Phase 4.2 (P0-8): how long an audit row is kept, and — because the
# append-only trigger keys off the resulting timestamp (see
# alembic/versions/a0b1c2d3e4f5) — how long it is *undeletable* for.
#
# Deliberately far longer than PHI retention (AUDIO_RETENTION_DAYS
# defaults to 90). The audit trail is the evidence used to answer "who
# looked at this patient's record?" during a breach investigation or an
# NPC complaint, and that question is almost always asked about records
# that have themselves already been deleted. An audit log that expires
# with the PHI it describes cannot answer it.
#
# 7 years is a placeholder with a rationale, not a legal finding: it
# outlives the longest plausible complaint/investigation window by a
# wide margin while staying a bounded commitment. The PRD's retention
# question is still owned by Legal/Compliance — see
# docs/decisions/0032.
DEFAULT_AUDIT_LOG_RETENTION_DAYS = 2555  # 7 years


def audit_log_retention_days() -> int:
    """The configured audit-log retention window, in days.

    Read through `getattr` rather than as a plain `settings.` attribute
    because `app/core/config.py` is owned by a concurrent phase (4.3) and
    could not take a new field this phase. The moment
    `audit_log_retention_days` lands in `Settings`, this picks it up with
    no change here; until then the constant above is the policy. See
    docs/progress/4.2-audit-logging.md's open follow-ups.
    """
    return int(getattr(get_settings(), "audit_log_retention_days", DEFAULT_AUDIT_LOG_RETENTION_DAYS))


def default_retention_expires_at() -> datetime:
    """Stamped on every row at insert time, by the column default, so a
    row written outside `app/services/audit.py:record` (a test, a future
    background job) still carries a retention date. A NULL here would be
    read by the trigger as "never expires" and by 4.4's purge job as
    "skip" — both fail safe, but neither is the policy.
    """
    return datetime.now(timezone.utc) + timedelta(days=audit_log_retention_days())


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """P0-8: "Access and change logs retained and reviewable." One table,
    written through a single helper (app/services/audit.py:record) rather
    than ad-hoc logging calls scattered per route, so every write goes
    through one code path with a consistent shape.

    **No PHI in this table, ever.** Not a patient name, not note text, not
    an S3 object key. A row here outlives the record it describes by years
    (see DEFAULT_AUDIT_LOG_RETENTION_DAYS), and the trigger installed in
    Phase 4.2 means it cannot be edited or deleted to take PHI back out.
    Anything written here is written permanently. Entity *ids* are stored
    because they are surrogate keys with no meaning outside this database
    and because the trail is useless without them.
    """

    __tablename__ = "audit_logs"

    actor_clinician_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clinicians.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "note.sign", "patient.read"
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded before/after, when applicable

    # Phase 4.2: when this row may be purged (4.4 owns the job that does
    # the purging — this column is the policy it reads). NOT NULL and
    # stamped at insert so the value is fixed by the policy in force when
    # the access happened; a later policy change does not retroactively
    # shorten the life of rows already written, which is the property that
    # makes "we keep access logs for N years" a promise rather than a
    # setting someone can turn down after the fact.
    retention_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=default_retention_expires_at, nullable=False
    )

    # The three questions the review interface (P0-8's "reviewable") is
    # actually asked, each given the index that answers it:
    #   "who touched this record?"      -> (entity_type, entity_id, created_at)
    #   "what did this clinician do?"   -> (actor_clinician_id, created_at)
    #   "who did X, and when?"          -> (action, created_at)
    # Declared on the model, not only in the migration, so SQLite's
    # create_all builds the same indexes the deploy target has — the exact
    # divergence tests/test_postgres_specific.py exists to police.
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_logs_actor", "actor_clinician_id", "created_at"),
        Index("ix_audit_logs_action", "action", "created_at"),
        # The purge job's own scan (4.4). Without it, "delete everything
        # expired" is a full table scan of the largest table in the system.
        Index("ix_audit_logs_retention_expires_at", "retention_expires_at"),
    )
