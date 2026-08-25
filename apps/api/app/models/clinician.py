from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Clinician(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A clinician account. `role` drives the need-to-know RBAC checks in
    app/api/deps.py (P0-8). `prc_license_number` is required at signing
    time (P0-5), not at account creation, since not every role holds one.
    """

    __tablename__ = "clinicians"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="doctor")  # doctor | compliance | admin
    prc_license_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # TOTP secret for MFA (P0-8). Stored as-is behind a DB-level access
    # control, not EncryptedString, since it's a credential, not PHI —
    # revisit alongside the KMS decision in docs/tech-stack.md §9 if
    # secret-at-rest handling needs to be stricter than the app boundary.
    mfa_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Phase 0.3: holds a freshly-provisioned secret between "enroll"
    # (generate + show the QR) and "confirm" (prove one valid code) —
    # see app/api/routes/auth.py's /mfa/enroll* pair. Never read at
    # login time; only `mfa_secret` (below) is. This is what makes
    # "provision secret -> confirm before activating" real: an
    # enrollment that's never confirmed leaves login exactly as
    # unable-to-MFA as before it started.
    mfa_secret_pending: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
