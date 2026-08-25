"""encounter.audio_upload_id (Phase 1.1: presigned multipart upload)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-25 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("encounters", sa.Column("audio_upload_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("encounters", "audio_upload_id")
