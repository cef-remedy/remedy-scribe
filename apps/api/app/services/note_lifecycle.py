"""The only code path allowed to change Note.status. Routes must call
transition() rather than assigning note.status directly, so "no state
skippable" (P0-5) is enforced in one place instead of trusted to every
caller.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.encounter import Encounter
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


class PatientIdentityNotConfirmedError(Exception):
    """P0-6: "Patient identity is re-confirmed at the moment a note is
    filed, not only at recording start."

    Raised when a note is filed without the caller naming the patient it
    belongs to. This is a *state* problem, so routes map it to 409.
    """


def transition(
    db: Session,
    note: Note,
    to_status: NoteStatus,
    *,
    clinician_id: str,
    prc_license_number: str | None = None,
    confirmed_patient_id: str | None = None,
) -> Note:
    """Advances the note exactly one step.

    `confirmed_patient_id` is required for the FILED transition and is
    checked against the encounter's current `patient_id` (Phase 2.5, P0-6).
    Filing is the point the note becomes part of a patient's record, so it
    is the last moment a mis-linked recording can be caught cheaply — after
    that the note is in the wrong person's history. Requiring the caller to
    *name* the patient, rather than reading `encounter.patient_id` and
    trusting it, is what makes this a confirmation rather than a formality:
    a stale client showing the previous patient will disagree and be
    rejected.
    """
    allowed = _ALLOWED_NEXT.get(note.status)
    if allowed is None or to_status != allowed:
        raise InvalidTransitionError(
            f"Cannot move note {note.id} from {note.status.value} to {to_status.value}; "
            f"only {note.status.value} -> {allowed.value if allowed else '(terminal)'} is allowed."
        )

    if to_status == NoteStatus.FILED:
        encounter = db.get(Encounter, note.encounter_id)
        actual_patient_id = encounter.patient_id if encounter else None
        if actual_patient_id is None:
            raise PatientIdentityNotConfirmedError(
                "This recording is not linked to a patient yet. Link it before filing the note."
            )
        if confirmed_patient_id is None:
            raise PatientIdentityNotConfirmedError(
                "Filing requires confirming which patient this note belongs to."
            )
        if confirmed_patient_id != actual_patient_id:
            # The client believed a different patient than the encounter
            # holds. Never silently prefer one: that is precisely how a
            # note lands in the wrong person's record.
            raise PatientIdentityNotConfirmedError(
                "The confirmed patient does not match the one this recording is linked to. "
                "Reload the note and check the patient before filing."
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
