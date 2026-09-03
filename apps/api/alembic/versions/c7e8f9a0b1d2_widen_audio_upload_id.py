"""Widen encounters.audio_upload_id for Google Drive session URIs.

Decision 0040. The column holds whatever the storage backend calls an
"upload id". For S3 that is a short opaque `UploadId`; for Drive it is the
entire **resumable session URI**, which is also the upload credential and
runs well past 128 characters.

A truncated URI does not fail at write time — it fails at the *first
chunk*, from the browser, with an error that names nothing useful. So the
column is widened before the backend that needs it can be selected, rather
than after someone spends an afternoon on a mysterious upload failure.

Widening only: no data is at risk, and every existing value fits.

Revision ID: c7e8f9a0b1d2
Revises: 6d6367a508f0
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "c7e8f9a0b1d2"
down_revision = "6d6367a508f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "encounters",
        "audio_upload_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=512),
        existing_nullable=True,
    )


def downgrade() -> None:
    # ⚠️ Narrowing can fail, and that is correct rather than unfortunate: if
    # any row holds a Drive session URI, this migration should refuse rather
    # than silently truncate the credential that makes an in-flight upload
    # resumable.
    op.alter_column(
        "encounters",
        "audio_upload_id",
        existing_type=sa.String(length=512),
        type_=sa.String(length=128),
        existing_nullable=True,
    )
