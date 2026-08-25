"""Phase 1.5: pipeline failure handling

Three additions, all on `encounters`:

- `pipeline_status` gains two terminal members — `transcription_failed`,
  `generation_failed` — so the CHECK constraint from d4e5f6a7b8c9 has to
  be dropped and recreated with the wider value set (no ALTER on a CHECK
  constraint; drop-and-recreate is the only path). Deliberately not
  adding `upload_failed` too — see docs/decisions/0023.
- `last_pipeline_error`: the last exception's message from a
  transcription/generation attempt, cleared on success.
- `pipeline_updated_at`: stamped on every pipeline_status transition,
  not a generic onupdate=now(). Backfilled from `created_at` for
  existing rows (the closest approximation available for "when did this
  row last make progress" before this column existed), then made
  NOT NULL — every row from here on sets it explicitly.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-25 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("encounters", sa.Column("last_pipeline_error", sa.String(length=500), nullable=True))
    op.add_column("encounters", sa.Column("pipeline_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE encounters SET pipeline_updated_at = created_at WHERE pipeline_updated_at IS NULL")
    op.alter_column("encounters", "pipeline_updated_at", nullable=False)

    op.drop_constraint("encounterpipelinestatus", "encounters", type_="check")
    op.create_check_constraint(
        "encounterpipelinestatus",
        "encounters",
        "pipeline_status IN ('recording', 'uploaded', 'transcribed', 'note_generated', "
        "'blocked_no_consent', 'transcription_failed', 'generation_failed')",
    )


def downgrade() -> None:
    op.drop_constraint("encounterpipelinestatus", "encounters", type_="check")
    op.create_check_constraint(
        "encounterpipelinestatus",
        "encounters",
        "pipeline_status IN ('recording', 'uploaded', 'transcribed', 'note_generated', 'blocked_no_consent')",
    )
    op.drop_column("encounters", "pipeline_updated_at")
    op.drop_column("encounters", "last_pipeline_error")
