"""The only code path allowed to change Note.status. Routes must call
transition() rather than assigning note.status directly, so "no state
skippable" (P0-5) is enforced in one place instead of trusted to every
caller.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.note import Note, NoteStatus

# Each status maps to the single next status it may advance to.
_ALLOWED_NEXT: dict[NoteStatus, NoteStatus] = {
    NoteStatus.GENERATED: NoteStatus.FILED,
    NoteStatus.FILED: NoteStatus.AUTHENTICATED,
    NoteStatus.AUTHENTICATED: NoteStatus.SIGNED,
}


class InvalidTransitionError(Exception):
    pass


class SigningRequiresLicenseError(Exception):
    pass


def transition(
    db: Session,
    note: Note,
    to_status: NoteStatus,
    *,
    clinician_id: str,
    prc_license_number: str | None = None,
) -> Note:
    allowed = _ALLOWED_NEXT.get(note.status)
    if allowed is None or to_status != allowed:
        raise InvalidTransitionError(
            f"Cannot move note {note.id} from {note.status.value} to {to_status.value}; "
            f"only {note.status.value} -> {allowed.value if allowed else '(terminal)'} is allowed."
        )

    if to_status == NoteStatus.SIGNED:
        # P0-5: "Signing captures doctor identity, PRC license number, and
        # timestamp in an audit trail."
        if not prc_license_number:
            raise SigningRequiresLicenseError("Signing requires a PRC license number.")
        note.signed_by_clinician_id = clinician_id
        note.signed_prc_license_number = prc_license_number
        note.signed_at = datetime.now(timezone.utc)

    note.status = to_status
    db.add(note)
    db.commit()
    db.refresh(note)
    return note
