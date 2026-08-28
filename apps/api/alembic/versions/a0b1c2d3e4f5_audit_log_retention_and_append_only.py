"""Phase 4.2: audit-log retention + append-only enforcement

Three things, all on `audit_logs`, all backing P0-8 ("access and change
logs retained and reviewable"):

1. `retention_expires_at`, NOT NULL. Retention stops being a document and
   becomes a column: 4.4's purge job reads it, and the trigger below keys
   off it. Backfilled for existing rows from `created_at`.

2. Three composite indexes for the review interface, plus one for the
   purge scan. Without them "who looked at this patient's record?" is a
   sequential scan of the fastest-growing table in the system, which is
   how a review interface becomes one nobody runs.

3. **Append-only enforcement, the same DB-layer pattern as the consent
   ledger** (`_append_only_consent_ledger.py`) and for the same reason: a
   REVOKE would not hold, because the app's own DB role owns the table it
   created and table owners bypass GRANT/REVOKE in Postgres. A trigger
   that RAISEs applies to the owner too, so this survives a full
   compromise of the API's database credentials — which is precisely the
   scenario in which someone would want to edit the access log.

   Two differences from the consent ledger's version, both deliberate:

   - **DELETE is permitted once `retention_expires_at` has passed.** The
     consent ledger blocks every mutation forever; an audit log cannot,
     because it has a retention period and something has to enforce it.
     Rather than granting the purge job a superuser escape hatch (which
     would also be an escape hatch for anyone who steals its
     credentials), the trigger encodes the policy: before the expiry
     date, nobody may delete the row — not the app, not the purge job,
     not a compromised credential. After it, the ordinary purge succeeds.
     UPDATE stays blocked unconditionally, which is what makes this safe:
     `retention_expires_at` itself cannot be moved forward to bring a row
     into deletable range.

   - **TRUNCATE is blocked too**, via a statement-level trigger. Row-level
     triggers do not fire on TRUNCATE, so `TRUNCATE audit_logs` would
     otherwise erase the entire access log while leaving the "append-only"
     guarantee technically intact. (The consent ledger has this same gap;
     see docs/progress/4.2-audit-logging.md's follow-ups — fixing it is a
     one-line migration owned by whoever revisits P0-1.)

Revision ID: a0b1c2d3e4f5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-28 10:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a0b1c2d3e4f5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

# Mirrors app/models/audit_log.py's DEFAULT_AUDIT_LOG_RETENTION_DAYS at the
# time this migration was written. Written as a literal rather than
# imported, because a migration must keep producing the same result when
# it is replayed years from now against an empty database — importing the
# live constant would make this historical backfill silently change
# meaning the day someone edits the policy.
_BACKFILL_RETENTION_DAYS = 2555  # 7 years


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True))
    # Existing rows get the policy applied from when they were written, not
    # from today: an access logged last year expires a year before one
    # logged today, which is what a retention period means.
    op.execute(
        f"UPDATE audit_logs SET retention_expires_at = created_at + INTERVAL '{_BACKFILL_RETENTION_DAYS} days' "
        "WHERE retention_expires_at IS NULL"
    )
    op.alter_column("audit_logs", "retention_expires_at", nullable=False)

    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id", "created_at"])
    op.create_index("ix_audit_logs_actor", "audit_logs", ["actor_clinician_id", "created_at"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action", "created_at"])
    op.create_index("ix_audit_logs_retention_expires_at", "audit_logs", ["retention_expires_at"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_audit_log_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            -- The single permitted mutation: deleting a row whose
            -- retention period has already elapsed (Phase 4.4's purge).
            -- Everything else, including any UPDATE, is refused.
            IF TG_OP = 'DELETE' AND OLD.retention_expires_at <= now() THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION
                'audit_logs is append-only (P0-8) — % is not permitted before retention_expires_at', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_mutation
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_audit_log_truncate()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only (P0-8) — TRUNCATE is not permitted';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_log_truncate();
        """
    )


def downgrade() -> None:
    # Triggers first: with them in place, nothing below could touch the
    # table. Note that a downgrade is itself a way to make the audit log
    # mutable again — DDL is not covered by these triggers and cannot be
    # (dropping a trigger is an owner privilege). The control against that
    # is that migrations are an explicit deploy step (checklist 5.1), not
    # something the running application can do.
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_log_truncate")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_mutation ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_log_mutation")

    op.drop_index("ix_audit_logs_retention_expires_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity", table_name="audit_logs")
    op.drop_column("audit_logs", "retention_expires_at")
