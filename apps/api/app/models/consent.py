from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ConsentLedgerEntry(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """P0-1: "Immutable, append-only consent ledger records participant
    roster, purposes consented to, timestamp, and language of the script
    for every encounter."

    Immutability is enforced at the DB layer, not just by convention: a
    later migration (alembic/versions/..._append_only_consent_ledger.py)
    adds a BEFORE UPDATE/DELETE trigger that raises unconditionally —
    this holds even for the table's owning role, which a plain REVOKE
    would not. `created_at` (from TimestampMixin) is the ledger
    timestamp; there is deliberately no `updated_at`.

    `event` distinguishes given / declined / withdrawn per the Compliance
    and Patient user stories. A withdrawal is a new row, never an edit to
    the original consent row.
    """

    __tablename__ = "consent_ledger_entries"

    encounter_id: Mapped[str] = mapped_column(String(36), ForeignKey("encounters.id"), nullable=False, index=True)

    event: Mapped[str] = mapped_column(String(16), nullable=False)  # given | declined | withdrawn
    participant_roster: Mapped[str] = mapped_column(String(2048), nullable=False)  # JSON-encoded list of roles/names
    purposes: Mapped[str] = mapped_column(String(512), nullable=False)  # JSON-encoded list
    script_language: Mapped[str] = mapped_column(String(8), nullable=False)  # "fil" | "en"
