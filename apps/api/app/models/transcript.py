from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import EncryptedJSON
from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Transcript(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Phase 1.2: `transcribe_encounter`'s actual output, finally kept
    instead of discarded (`_ = segments`), and what `generate_note` now
    loads instead of always passing `transcript=[]`.

    One row per encounter. `segments` holds the full diarized,
    word-timed, confidence-scored structure `ASRProvider.transcribe`
    returns — encrypted as one JSON blob (EncryptedJSON; see its
    docstring for why "JSONB" here means shape, not Postgres's native
    type) rather than normalized into a row-per-word table. Each
    segment carries a stable string `id` (`"seg0"`, `"seg1"`, ...) —
    today that's just "one ASR-diarized turn", not yet a grammatical
    sentence (turn-splitting itself is still Phase 1.3's fix, and
    sentence-level citation is still Phase 1.4's open decision); this
    schema doesn't presuppose either, since re-deriving finer-grained
    units from `words`' own timings is always possible later without a
    migration.

    Shape of one entry in `segments`:
        {"id": "seg0", "speaker": "speaker_0",
         "words": [{"text": ..., "start_ms": ..., "end_ms": ...,
                     "confidence": ..., "speaker": ...}, ...]}

    Same PHI treatment as everything else in this schema: encrypted at
    rest (arguably more sensitive than the note itself — verbatim,
    including what the doctor chose not to write down), and its own
    retention clock mirroring the audio's (Phase 4.4 owns the deletion
    *job*; this column just gives it something to act on without a
    later migration).
    """

    __tablename__ = "transcripts"

    encounter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("encounters.id"), nullable=False, unique=True, index=True
    )
    asr_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # Phase 1.3: deferred in 1.2 (decision 0017) until a real ASR
    # integration existed to report a real version string — Groq's
    # GROQ_WHISPER_MODEL setting is that string now.
    asr_model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    segments: Mapped[list[dict]] = mapped_column(EncryptedJSON, nullable=False)
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
