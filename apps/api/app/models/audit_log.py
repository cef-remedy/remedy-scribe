from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class AuditLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """P0-8: "Access and change logs retained and reviewable." One table,
    written through a single helper (app/services/audit.py:record) rather
    than ad-hoc logging calls scattered per route, so every write goes
    through one code path with a consistent shape.
    """

    __tablename__ = "audit_logs"

    actor_clinician_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clinicians.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "note.sign", "patient.read"
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-encoded before/after, when applicable
