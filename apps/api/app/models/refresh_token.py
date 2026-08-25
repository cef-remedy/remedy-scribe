from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class RefreshToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Phase 0.3: pairs with the short-lived JWT access token to answer
    "what happens if the phone is lost?" — an access token is a
    self-contained, stateless JWT (app/core/security.py) that nothing
    server-side can revoke early; a refresh token is the opposite by
    design, an opaque random value whose *hash* (never the raw value)
    lives here, so a row can be flipped to revoked and the client can
    never silently mint another access token again.

    Rotation, not reuse: every successful refresh both validates the
    presented token and immediately retires it (`revoked_at` set,
    `replaced_by_id` pointing at its successor), issuing a new row. A
    client presenting an already-retired token is either racing itself
    (rare, harmless) or replaying a stolen token (the scenario this
    exists for) — see app/services/refresh_tokens.py, which treats any
    reuse as the latter and revokes the whole session family.
    """

    __tablename__ = "refresh_tokens"

    clinician_id: Mapped[str] = mapped_column(String(36), ForeignKey("clinicians.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("refresh_tokens.id"), nullable=True)
