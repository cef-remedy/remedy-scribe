from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LoginAttempt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Phase 0.3: append-only log of every POST /auth/login attempt,
    success or failure. Rate limiting and account lockout
    (app/services/auth_rate_limit.py) are both computed by folding a
    time-windowed slice of this table rather than maintaining a mutable
    counter — the same "history you read, not state you mutate and risk
    forgetting to reset" pattern already used for the consent ledger and
    audit log. A side effect worth knowing: a lockout clears itself
    exactly when its oldest counted failure ages out of the window —
    there's no separate "unlock" action or flag to get wrong.
    """

    __tablename__ = "login_attempts"

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    successful: Mapped[bool] = mapped_column(Boolean, nullable=False)
