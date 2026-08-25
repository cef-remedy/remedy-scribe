"""notes.prompt_version (Phase 1.4)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-25 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("prompt_version", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("notes", "prompt_version")
