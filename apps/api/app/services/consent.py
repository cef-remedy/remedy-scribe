"""Consent-gate enforcement (P0-1, Phase 0.1).

Before this module existed, nothing server-side stopped an encounter
from being uploaded and transcribed with zero consent records — the
rule lived only in the mobile client's UI, which is exactly where a
legal control must not live, since the client is the part a bug or an
attacker controls. This is the single place that answers "is it
currently legal to touch this encounter's audio?", so every enforcement
point (upload confirmation, the transcription task, anything added
later) shares one definition of "valid consent" instead of each
re-deriving it against the ledger.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.consent import ConsentLedgerEntry


class ConsentNotValidError(Exception):
    """Raised when an encounter has no currently-active consent.

    This is a *state* problem, not a *permissions* problem — the caller
    is allowed to be here, the ledger just doesn't support it yet (or
    any more). Route handlers should map this to 409, not 403.
    """


def assert_consent_valid(db: Session, encounter_id: str) -> None:
    """Raise ConsentNotValidError unless the most recent status implied
    by the ledger, read in chronological order, is "given".

    The ledger is append-only (enforced by a DB trigger — see
    alembic/versions/..._append_only_consent_ledger.py), so "current
    state" is never a column to read; it's always a fold over history.
    A "declined" or "withdrawn" event after a "given" one revokes it; a
    later "given" event (re-consent) restores it. No rows at all fails
    closed, same as a "declined" row would.
    """
    entries = (
        db.query(ConsentLedgerEntry)
        .filter(ConsentLedgerEntry.encounter_id == encounter_id)
        .order_by(ConsentLedgerEntry.created_at.asc())
        .all()
    )

    is_given = False
    for entry in entries:
        if entry.event == "given":
            is_given = True
        elif entry.event in ("declined", "withdrawn"):
            is_given = False

    if not is_given:
        raise ConsentNotValidError(f"Encounter {encounter_id} has no active consent record.")
