"""transcripts.asr_model_version (Phase 1.3)

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-25 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transcripts", sa.Column("asr_model_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("transcripts", "asr_model_version")
