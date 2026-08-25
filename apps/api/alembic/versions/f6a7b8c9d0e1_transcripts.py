"""transcripts (Phase 1.2: transcript persistence)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-25 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transcripts",
        sa.Column("encounter_id", sa.String(length=36), nullable=False),
        sa.Column("asr_provider", sa.String(length=32), nullable=False),
        sa.Column("segments", sa.Text(), nullable=False),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["encounter_id"], ["encounters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("encounter_id"),
    )
    op.create_index(op.f("ix_transcripts_encounter_id"), "transcripts", ["encounter_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_transcripts_encounter_id"), table_name="transcripts")
    op.drop_table("transcripts")
