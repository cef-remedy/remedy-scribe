from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.clinician import Clinician
from app.models.patient import Patient
from app.schemas.patient import (
    PatientLookupRequest,
    PatientMatchResult,
    PatientOut,
    PatientSearchHit,
    PriorVisitOut,
)
from app.services import audit
from app.services.patient_matching import (
    DEFAULT_SEARCH_LIMIT,
    create_patient,
    match_patient,
    previous_signed_note,
    search_patients_by_name,
)

router = APIRouter(prefix="/patients", tags=["patients"])


@router.post("/match", response_model=PatientMatchResult)
def match(
    payload: PatientLookupRequest,
    db: Session = Depends(get_db),
    # RBAC (0.2): patient search/match is a doctor action, part of the
    # same recording/identity workflow as encounters.
    clinician: Clinician = Depends(require_role("doctor")),
) -> PatientMatchResult:
    """P0-6: exact match links silently; near match needs a one-tap
    confirmation (client calls POST /patients with the confirmed name if
    the doctor picks a candidate, or accepts the exact match directly);
    no match means the client should create a new record.

    Audited as a PHI read (Phase 4.2). It is easy to read this route as a
    write path — it is a POST, and the client usually follows it with one —
    but it decrypts and ranks patient names to answer, and on an exact or
    near match it *discloses* an existing patient's identity to the caller.
    That is exactly the shape of access P0-8 asks to be accountable for.
    """
    result = match_patient(db, payload.name, payload.birthdate)
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="patient.match",
        entity_type="patient",
        # The matched patient when there is one; "*" when the answer was
        # "nobody" — which still read the directory to find out. The
        # submitted name and birthdate are deliberately not recorded: they
        # are PHI, and this table keeps what it is given for years (see
        # app/models/audit_log.py). `match_type` is not PHI and is what
        # makes the row interpretable later.
        entity_id=result.patient.id if result.patient is not None else "*",
        diff={"match_type": result.match_type, "candidate_count": len(result.candidates)},
    )
    return result


@router.post("", response_model=PatientOut, status_code=201)
def create(
    payload: PatientLookupRequest,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> PatientOut:
    """Creates a new record (P0-6: "no match creates a new record with
    name + birthdate"). Callers should have already called /match and
    confirmed match_type == "none" — this route does not re-check, since
    the one-tap confirmation UX for a near match also lands here with the
    doctor having explicitly chosen "this is a new patient."
    """
    patient = create_patient(db, payload.name, payload.birthdate)
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="patient.create",
        entity_type="patient",
        entity_id=patient.id,
    )
    return PatientOut.model_validate(patient)


@router.get("/search", response_model=list[PatientSearchHit])
def search(
    q: str = Query(min_length=1, max_length=200, description="Typed or dictated patient name"),
    limit: int = Query(DEFAULT_SEARCH_LIMIT, ge=1, le=50),
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> list[PatientSearchHit]:
    """Name-first fuzzy search (P0-6: "accepts a typed or dictated patient
    name and fuzzy-matches against the existing directory").

    Distinct from `POST /match`, which needs a birthdate and answers the
    *deduplication* question. This answers the earlier one: which patients
    might the doctor mean? Birthdate comes back with each hit so two people
    with similar names can be told apart, which is why it is stored
    unencrypted in the first place.

    Note the audit entry: this is a PHI read — it decrypts every patient
    name in the directory to rank them — so it is logged as one. Phase 4.2
    exists because reads are the ones developers forget.
    """
    hits = search_patients_by_name(db, q, limit=limit)
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="patient.search",
        entity_type="patient",
        # No single entity: a search reads the whole directory. The query
        # itself is deliberately NOT recorded — a patient name in an audit
        # log is PHI in a table with a longer retention than the record it
        # describes (Phase 4.2's own heads-up).
        entity_id="*",
    )
    return hits


@router.get("/{patient_id}/prior-visit", response_model=PriorVisitOut | None)
def prior_visit(
    patient_id: str,
    exclude_encounter_id: str | None = Query(None),
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> PriorVisitOut | None:
    """The prior visit's assessment and plan (P0-5's longitudinal-context
    item), for the note review screen.

    Returns null rather than 404 when there is no prior visit: a first-time
    patient is the normal case, not an error, and a 404 would push the client
    into treating an ordinary state as a failure.
    """
    note = previous_signed_note(db, patient_id, exclude_encounter_id=exclude_encounter_id)
    if note is None:
        return None

    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="note.read.prior_visit",
        entity_type="note",
        entity_id=note.id,
    )
    return PriorVisitOut(
        note_id=note.id,
        encounter_id=note.encounter_id,
        assessment=note.assessment,
        plan=note.plan,
        signed_at=note.signed_at,
    )


@router.get("/{patient_id}", response_model=PatientOut)
def get_patient(
    patient_id: str,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> PatientOut:
    """Resolves a patient id to a name (found by this redesign's completeness
    audit as a gap: NoteReview re-opens an encounter already linked to a
    patient and had no route to ask who that is, so the signing screen — the
    highest-stakes screen in the app — showed a truncated UUID instead of a
    name). Registered after `/search` on purpose: `/{patient_id}` would
    otherwise swallow that literal path.

    A PHI read like search and prior-visit above, so it is audited the same
    way.
    """
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Patient not found")

    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="patient.read",
        entity_type="patient",
        entity_id=patient.id,
    )
    return PatientOut.model_validate(patient)
