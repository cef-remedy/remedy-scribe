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

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.consent import ConsentLedgerEntry


class ConsentNotValidError(Exception):
    """Raised when an encounter has no currently-active consent.

    This is a *state* problem, not a *permissions* problem — the caller
    is allowed to be here, the ledger just doesn't support it yet (or
    any more). Route handlers should map this to 409, not 403.
    """


@dataclass(frozen=True)
class ConsentState:
    """The ledger folded into an answer. Phase 2.2 needs this as a *value*
    rather than only as an exception: P0-1 says the app must block
    recording when no consent exists, and to block it the client has to be
    able to ask. Local client state cannot answer it — a page reload mid-
    encounter loses that, while the ledger entry persists — so the read
    has to come from here.
    """

    encounter_id: str
    is_given: bool
    latest_event: str | None
    script_language: str | None
    entry_count: int


def current_consent_state(db: Session, encounter_id: str) -> ConsentState:
    """The single fold over the ledger. `assert_consent_valid` and the
    read endpoint both go through this, so "valid consent" keeps exactly
    one definition — the property this module's docstring promises.

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

    latest = entries[-1] if entries else None
    return ConsentState(
        encounter_id=encounter_id,
        is_given=is_given,
        latest_event=latest.event if latest else None,
        script_language=latest.script_language if latest else None,
        entry_count=len(entries),
    )


def assert_consent_valid(db: Session, encounter_id: str) -> None:
    """Raise ConsentNotValidError unless the ledger currently implies
    "given". Thin wrapper over `current_consent_state` on purpose: the
    enforcement points (upload confirmation, the transcription task) and
    the client-facing read must never be able to disagree.
    """
    if not current_consent_state(db, encounter_id).is_given:
        raise ConsentNotValidError(f"Encounter {encounter_id} has no active consent record.")
