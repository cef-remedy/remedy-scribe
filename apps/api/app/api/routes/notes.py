from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_clinician, get_db, require_role
from app.models.clinician import Clinician
from app.models.note import Note
from app.schemas.grounding import GroundingOut
from app.schemas.note import NoteOut, NoteSectionUpdate, NoteTransitionRequest
from app.services import audit
from app.services.grounding import resolve_grounding
from app.services.note_lifecycle import (
    InvalidTransitionError,
    PatientIdentityNotConfirmedError,
    SigningRequiresLicenseError,
    transition,
)

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


@router.get("/{note_id}/grounding", response_model=GroundingOut)
def read_grounding(
    note_id: str,
    db: Session = Depends(get_db),
    # Same read scope as the note itself (decision 0004): grounding is a
    # view *of* the note, and gating it more tightly than the note would
    # mean the people allowed to read a note are not allowed to check it.
    clinician: Clinician = Depends(get_current_clinician),
) -> GroundingOut:
    """Phase 3 (P0-7): everything needed to answer "where did this line come
    from?" in one read.

    Resolves each section's stored spans against the note's *current* text,
    returns the cited transcript passages with their audio timestamps, and
    reports which rung of the degradation ladder this encounter is on
    (audio + transcript, transcript only, or neither). See
    app/services/grounding.py for why each of those is verified rather than
    assumed.

    Audited separately from `note.read`: reading a note is reading the
    clinician-facing summary, while this returns verbatim transcript
    passages — a strictly larger PHI disclosure, and one worth being able
    to account for on its own.
    """
    note = _get_note_or_404(db, note_id)
    grounding = resolve_grounding(db, note)
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="note.grounding.read",
        entity_type="note",
        entity_id=note.id,
    )
    return GroundingOut.model_validate(grounding)


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

    # Phase 4.2: this was the one *change* to clinical content in the whole
    # API that wrote no audit row. A NoteRevision was written (P0-5's
    # edit-burden metric) and that is a change record of a sort, but it is
    # not the audit trail: it is deleted alongside the note under retention
    # (4.4), it holds the before/after PHI text, and it is not visible to
    # the compliance review interface. "Access and change logs" means both.
    #
    # Only the section name goes in the diff. The before/after text is the
    # note itself — it lives in note_revisions, under the note's own
    # retention, which is where PHI belongs.
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="note.edit",
        entity_type="note",
        entity_id=note.id,
        diff={"section": payload.section},
    )
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
            confirmed_patient_id=payload.confirmed_patient_id,
        )
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except SigningRequiresLicenseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except PatientIdentityNotConfirmedError as exc:
        # A state problem, not a permissions one: the caller may file,
        # the note just is not attached to a confirmed patient yet.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action=f"note.transition.{payload.to_status.value}",
        entity_type="note",
        entity_id=note.id,
    )
    return NoteOut.model_validate(note)
