from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Encounter(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One recorded consultation. `patient_id` is nullable: P0-6 requires
    recording never be blocked on identity — an encounter with no
    patient_id sits in the "loose sessions" tray until linked.

    `upload_idempotency_key` backs P0-2 ("uploads are resumable and
    chunked, with an idempotency key that prevents duplicate notes from a
    retried upload") — unique, so a retried final-chunk request resolves
    to this same row via get-or-create instead of spawning a second
    pipeline run.

    `audio_retention_expires_at` implements the Compliance story
    ("audio retention duration is a configurable value") — computed at
    upload-confirmation time from settings.audio_retention_days, not
    hardcoded, and re-computable per-encounter if the policy changes.
    """

    __tablename__ = "encounters"

    patient_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("patients.id"), nullable=True, index=True
    )
    clinician_id: Mapped[str] = mapped_column(String(36), ForeignKey("clinicians.id"), nullable=False)

    upload_idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    audio_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)  # S3 key, set once uploaded
    audio_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audio_retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # generated | filed | authenticated | signed — mirrors Note.status but
    # kept on the encounter too so "loose sessions" / queue-status queries
    # don't need a join when there's no Note row yet (pre-transcription).
    pipeline_status: Mapped[str] = mapped_column(String(32), nullable=False, default="recording")

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
