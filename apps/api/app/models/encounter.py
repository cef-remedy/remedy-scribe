import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class EncounterPipelineStatus(str, enum.Enum):
    """Phase 0.4: was a free-form String(32) that the codebase wrote at
    least five different values into across two files (encounters.py,
    tasks/pipeline.py) with nothing checking any of them matched. Now a
    proper enum, the way Note.status already was in name — see the
    `create_constraint=True` below for why "already was" turned out to
    be only half true.
    """

    RECORDING = "recording"
    UPLOADED = "uploaded"
    TRANSCRIBED = "transcribed"
    NOTE_GENERATED = "note_generated"
    BLOCKED_NO_CONSENT = "blocked_no_consent"  # app/services/consent.py's terminal state (0.1)

    # Phase 1.5 will add more members here (upload_failed,
    # transcription_failed, generation_failed, ...) for per-failure-mode
    # error states — additive, not a reason to redesign this enum.


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
    audio_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)  # S3 key, server-generated (1.1)
    # S3 multipart UploadId (Phase 1.1) — set at upload/init, cleared at
    # upload/complete. Its presence is what makes both endpoints
    # idempotent: init returns the existing key/upload_id on retry
    # instead of orphaning a second S3-side session, and complete treats
    # a missing upload_id (already-completed encounter) as a no-op
    # rather than re-calling S3 with a since-consumed UploadId.
    audio_upload_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    audio_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audio_retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Mirrors Note.status's role but kept on the encounter too so "loose
    # sessions" / queue-status queries don't need a join when there's no
    # Note row yet (pre-transcription). native_enum=False keeps this a
    # plain VARCHAR under the hood (portable to SQLite, like
    # UUIDPrimaryKeyMixin's String(36) ids); create_constraint=True is
    # what actually makes "the database physically cannot hold an
    # invalid value" true — SQLAlchemy 2.0 does NOT add that CHECK by
    # default for a non-native enum, confirmed empirically while fixing
    # this (see docs/decisions/0010).
    pipeline_status: Mapped[EncounterPipelineStatus] = mapped_column(
        Enum(
            EncounterPipelineStatus,
            native_enum=False,
            create_constraint=True,
            name="encounterpipelinestatus",
            # See app/models/note.py's identical comment: without this,
            # the generated CHECK constraint lists member NAMES
            # ("RECORDING") instead of the VALUES ("recording") actually
            # written to the column, and rejects every real write.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=EncounterPipelineStatus.RECORDING,
    )

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
