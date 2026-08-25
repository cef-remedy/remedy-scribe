from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_clinician, get_db, require_role
from app.models.clinician import Clinician
from app.models.note import Note
from app.schemas.note import NoteOut, NoteSectionUpdate, NoteTransitionRequest
from app.services import audit
from app.services.note_lifecycle import InvalidTransitionError, SigningRequiresLicenseError, transition

router = APIRouter(prefix="/notes", tags=["notes"])


def _get_note_or_404(db: Session, note_id: str) -> Note:
    note = db.get(Note, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return note


@router.get("/{note_id}", response_model=NoteOut)
def get_note(
    note_id: str,
    db: Session = Depends(get_db),
    # RBAC (0.2): reads are deliberately open to any authenticated
    # clinician (doctor for continuity of care across colleagues,
    # compliance for review sampling) — need-to-know is enforced by
    # making every read accountable via audit.record below, not by
    # blocking. See docs/decisions/0004-note-read-access-scope.md.
    clinician: Clinician = Depends(get_current_clinician),
) -> NoteOut:
    note = _get_note_or_404(db, note_id)
    audit.record(db, actor_clinician_id=clinician.id, action="note.read", entity_type="note", entity_id=note.id)
    return NoteOut.model_validate(note)


@router.patch("/{note_id}", response_model=NoteOut)
def edit_section(
    note_id: str,
    payload: NoteSectionUpdate,
    db: Session = Depends(get_db),
    # RBAC (0.2): only the treating doctor edits clinical content —
    # compliance is read/audit-only, admin is a system role, neither
    # writes PHI clinical text.
    clinician: Clinician = Depends(require_role("doctor")),
) -> NoteOut:
    """P0-5: "Doctor can freely edit any section before signing; edits are
    tracked for the edit-burden metric." Every edit writes a NoteRevision
    regardless of size — the edit-burden metric needs the raw edit
    history, not just the final diff from generation.
    """
    note = _get_note_or_404(db, note_id)
    if note.status.value == "signed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot edit a signed note")

    from app.models.note import NoteRevision  # local import: keeps notes.py's top-level imports to what every route needs

    previous_text = getattr(note, payload.section)
    db.add(
        NoteRevision(
            note_id=note.id,
            section=payload.section,
            previous_text=previous_text,
            new_text=payload.text,
            edited_by_clinician_id=clinician.id,
        )
    )
    setattr(note, payload.section, payload.text)
    db.add(note)
    db.commit()
    db.refresh(note)
    return NoteOut.model_validate(note)


@router.post("/{note_id}/transition", response_model=NoteOut)
def transition_note(
    note_id: str,
    payload: NoteTransitionRequest,
    db: Session = Depends(get_db),
    # RBAC (0.2): filing/authenticating/signing are doctor actions;
    # signing in particular binds a PRC license number to a real
    # clinician identity, which only "doctor" accounts should be able
    # to attest to.
    clinician: Clinician = Depends(require_role("doctor")),
) -> NoteOut:
    """Drives the P0-5 state machine one step at a time. Signing
    (to_status == "signed") additionally requires prc_license_number and
    is recorded in the audit trail with the clinician's identity.
    """
    note = _get_note_or_404(db, note_id)
    try:
        note = transition(
            db,
            note,
            payload.to_status,
            clinician_id=clinician.id,
            prc_license_number=payload.prc_license_number,
        )
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except SigningRequiresLicenseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action=f"note.transition.{payload.to_status.value}",
        entity_type="note",
        entity_id=note.id,
    )
    return NoteOut.model_validate(note)
