from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_clinician, get_db, require_role
from app.models.clinician import Clinician
from app.models.encounter import Encounter
from app.models.note import Note, NoteStatus
from app.schemas.grounding import GroundingOut
from app.schemas.note import NoteOut, NoteSearchRow, NoteSectionUpdate, NoteTransitionRequest
from app.services import audit
from app.services.grounding import resolve_grounding
from app.services.note_lifecycle import (
    InvalidTransitionError,
    PatientIdentityNotConfirmedError,
    transition,
)
from app.services.patient_matching import decrypt_patient_names, name_matches

router = APIRouter(prefix="/notes", tags=["notes"])

# A bounded window, not the whole table: `q` cannot be pushed down into SQL
# (Patient.full_name is encrypted, non-deterministic ciphertext -- see
# patient_matching.py's own heads-up on this), so a name search decrypts
# candidates in Python instead. Scanning the entire history to answer one
# search would get slower every week the clinic operates; this caps it at
# the most recent MAX_NAME_SCAN notes, same tradeoff decision 0029 already
# accepted for patient-name search.
MAX_NAME_SCAN = 1000
DEFAULT_SEARCH_PAGE_SIZE = 50
MAX_SEARCH_PAGE_SIZE = 200


def _get_note_or_404(db: Session, note_id: str) -> Note:
    note = db.get(Note, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return note


@router.get("/search", response_model=list[NoteSearchRow])
def search_notes(
    response: Response,
    q: str | None = Query(None, min_length=1, max_length=200, description="Typed patient name"),
    status_filter: NoteStatus | None = Query(None, alias="status"),
    date_from: datetime | None = Query(None, description="Inclusive lower bound on the encounter's created_at"),
    date_to: datetime | None = Query(None, description="Exclusive upper bound on the encounter's created_at"),
    limit: int = Query(DEFAULT_SEARCH_PAGE_SIZE, ge=1, le=MAX_SEARCH_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    # RBAC (decision 0004): the same read scope as GET /{note_id} below --
    # a browsable history of notes is a read like any other, open to any
    # authenticated clinician for continuity of care, not scoped to only
    # the doctor who recorded each one (unlike /encounters/recent, which
    # is deliberately "my own worklist").
    clinician: Clinician = Depends(get_current_clinician),
) -> list[NoteSearchRow]:
    """The "All notes" page.

    Missing for the same reason `/encounters/recent` was: the only lists
    were Recent (this clinician's last 25 *encounters*), Loose, and
    Failed -- nothing let a doctor find a note from two weeks ago, search
    by patient, or reach anything a colleague filed. This is the first
    endpoint that lists *notes* rather than encounters, and the first
    that is genuinely searchable/paginated.

    Registered before `/{note_id}` on purpose -- same route-order
    constraint `encounters.py` documents: a path parameter registered
    first would swallow the literal `/search` segment.
    """
    query = (
        db.query(Note, Encounter)
        .join(Encounter, Note.encounter_id == Encounter.id)
        .order_by(Encounter.created_at.desc(), Note.id.desc())
    )
    if status_filter is not None:
        query = query.filter(Note.status == status_filter)
    if date_from is not None:
        query = query.filter(Encounter.created_at >= date_from)
    if date_to is not None:
        query = query.filter(Encounter.created_at < date_to)

    if q:
        # Cannot filter this in SQL -- decrypt a bounded candidate window
        # and match in Python, same approach patient_matching.search_
        # patients_by_name uses and for the same reason.
        candidates = query.limit(MAX_NAME_SCAN).all()
        candidate_patient_ids = {enc.patient_id for _, enc in candidates if enc.patient_id is not None}
        names = decrypt_patient_names(db, candidate_patient_ids)
        matched_ids = {pid for pid, name in names.items() if name and name_matches(name, q)}
        filtered = [(note, enc) for note, enc in candidates if enc.patient_id in matched_ids]
        total = len(filtered)
        page = filtered[offset : offset + limit]
    else:
        total = query.count()
        page = [(note, enc) for note, enc in query.limit(limit).offset(offset).all()]
        page_patient_ids = {enc.patient_id for _, enc in page if enc.patient_id is not None}
        names = decrypt_patient_names(db, page_patient_ids)

    # Same pagination-over-headers shape as GET /audit-logs: the body stays
    # a bare array so nothing generated against it breaks if pagination is
    # added to more endpoints later.
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)

    # A list read is still a read (Phase 4.2) -- same reasoning as
    # /encounters/recent and /loose: entity_id is "*" because there is no
    # single subject, and the typed query itself is deliberately not
    # recorded, since a patient-name search term is PHI (the same rule
    # patient.search follows for its own query string).
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="note.search",
        entity_type="note",
        entity_id="*",
        diff={"has_query": bool(q), "status": status_filter.value if status_filter else None, "result_count": total},
    )

    return [
        NoteSearchRow(
            note_id=note.id,
            encounter_id=enc.id,
            patient_id=enc.patient_id,
            patient_name=names.get(enc.patient_id) if enc.patient_id else None,
            note_status=note.status,
            pipeline_status=enc.pipeline_status,
            created_at=enc.created_at,
            signed_at=note.signed_at,
        )
        for note, enc in page
    ]


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
    # RBAC (0.2): filing/authenticating/signing are doctor actions; signing
    # in particular binds a clinician identity to the note, which only
    # "doctor" accounts should be able to attest to.
    clinician: Clinician = Depends(require_role("doctor")),
) -> NoteOut:
    """Drives the P0-5 state machine one step at a time. Signing
    (to_status == "signed") is recorded in the audit trail with the
    clinician's identity.
    """
    note = _get_note_or_404(db, note_id)
    try:
        note = transition(
            db,
            note,
            payload.to_status,
            clinician_id=clinician.id,
            confirmed_patient_id=payload.confirmed_patient_id,
        )
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
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
