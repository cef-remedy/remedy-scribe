"""type/consistency drift: CHECK constraints for status/event columns

Phase 0.4. Three columns that were "an enum" in name only get an actual
DB-level CHECK constraint here:

- notes.status: NoteStatus was already a Python enum (Enum column type,
  native_enum=False) but never had `create_constraint=True` -- as of
  SQLAlchemy 2.0 that flag defaults to False, so this table has had NO
  DB-level constraint on `status` since the very first migration despite
  the model's docstring claiming otherwise. Confirmed empirically (see
  docs/decisions/0010) before writing this migration, not assumed.
- encounters.pipeline_status: was a free-form String(32); the codebase
  wrote five distinct values into it across two files with nothing
  checking any of them.
- consent_ledger_entries.event: was a String(16) with only a Python
  comment documenting the allowed values.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-25 10:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "notestatus",
        "notes",
        "status IN ('generated', 'filed', 'authenticated', 'signed')",
    )
    op.create_check_constraint(
        "encounterpipelinestatus",
        "encounters",
        "pipeline_status IN ('recording', 'uploaded', 'transcribed', 'note_generated', 'blocked_no_consent')",
    )
    op.create_check_constraint(
        "consenteventtype",
        "consent_ledger_entries",
        "event IN ('given', 'declined', 'withdrawn')",
    )


def downgrade() -> None:
    op.drop_constraint("consenteventtype", "consent_ledger_entries", type_="check")
    op.drop_constraint("encounterpipelinestatus", "encounters", type_="check")
    op.drop_constraint("notestatus", "notes", type_="check")
