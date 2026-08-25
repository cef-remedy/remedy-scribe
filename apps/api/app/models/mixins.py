import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UUIDPrimaryKeyMixin:
    """String(36) UUID PK — portable across Postgres and SQLite (test DB),
    unlike postgresql.UUID which only works against a live Postgres.
    """

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid_str)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
