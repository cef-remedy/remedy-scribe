import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import EncryptedString
from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class NoteStatus(str, enum.Enum):
    """P0-5: "Note state machine enforces four distinct states: generated
    → filed → authenticated → signed, with no state skippable."

    The Enum column type below constrains the DB to these four values;
    the *ordering* (no skipping) is enforced in
    app/services/note_lifecycle.py, which is the only code path allowed
    to write Note.status — routes must go through it rather than setting
    status directly.
    """

    GENERATED = "generated"
    FILED = "filed"
    AUTHENTICATED = "authenticated"
    SIGNED = "signed"


class Note(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Structured note, ordered Assessment → Plan → Subjective → Objective
    per P0-4. Each section is stored separately (rather than one blob) so
    the grounding UI (P0-7) and edit-burden diffing (Success Metrics) can
    address a section independently.

    `assessment_source_spans` etc. hold JSON-encoded transcript-offset
    references — "every generated line is traceable back to its source
    transcript passage" (P0-4) — consumed by the grounding UI (P0-7).
    """

    __tablename__ = "notes"

    encounter_id: Mapped[str] = mapped_column(String(36), ForeignKey("encounters.id"), nullable=False, unique=True)
    status: Mapped[NoteStatus] = mapped_column(
        Enum(NoteStatus, native_enum=False), nullable=False, default=NoteStatus.GENERATED
    )

    assessment: Mapped[str] = mapped_column(EncryptedString(4096), nullable=False, default="")
    plan: Mapped[str] = mapped_column(EncryptedString(4096), nullable=False, default="")
    subjective: Mapped[str] = mapped_column(EncryptedString(4096), nullable=False, default="")
    objective: Mapped[str] = mapped_column(EncryptedString(4096), nullable=False, default="")

    # JSON-encoded {section: [{start, end, transcript_start_ms, transcript_end_ms}, ...]}
    source_spans: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    note_generator_provider: Mapped[str] = mapped_column(String(16), nullable=False)  # "luna" | "haiku"

    # Signing (P0-5): captured at the moment status -> SIGNED.
    signed_by_clinician_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clinicians.id"), nullable=True)
    signed_prc_license_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NoteRevision(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One row per doctor edit before signing — the raw material for the
    edit-burden success metric (% minor-edit-only, median edit distance).
    Written by app/services/note_lifecycle.py on every PATCH to a note,
    not just on save/submit, so edit distance reflects actual editing
    behavior.
    """

    __tablename__ = "note_revisions"

    note_id: Mapped[str] = mapped_column(String(36), ForeignKey("notes.id"), nullable=False, index=True)
    section: Mapped[str] = mapped_column(String(16), nullable=False)  # assessment | plan | subjective | objective
    previous_text: Mapped[str] = mapped_column(EncryptedString(4096), nullable=False)
    new_text: Mapped[str] = mapped_column(EncryptedString(4096), nullable=False)
    edited_by_clinician_id: Mapped[str] = mapped_column(String(36), ForeignKey("clinicians.id"), nullable=False)
