from datetime import date

from sqlalchemy import Date, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import EncryptedString
from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Patient(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """PRD P0-6: dedup uses name + birthdate together, never name alone —
    see app/services/patient_matching.py for the fuzzy-match + dedupe logic
    that enforces this at lookup time, not just at insert time.

    full_name is PHI and stored via EncryptedString; birthdate is kept
    plaintext because it's needed as a fast, indexable dedupe key — encrypt
    it too if the DPO's eventual retention/PHI policy requires it, at the
    cost of losing the DB index.
    """

    __tablename__ = "patients"

    full_name: Mapped[str] = mapped_column(EncryptedString(512), nullable=False)
    birthdate: Mapped[date] = mapped_column(Date, nullable=False, index=True)
