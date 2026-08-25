import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_clinician, get_db
from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.schemas.consent import ConsentEntryCreate, ConsentEntryOut

router = APIRouter(prefix="/consent", tags=["consent"])


@router.post("", response_model=ConsentEntryOut, status_code=201)
def record_consent_event(
    payload: ConsentEntryCreate,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(get_current_clinician),
) -> ConsentEntryOut:
    """P0-1: appends one row to the immutable consent ledger. Never
    updates a prior row — "given", "declined", and "withdrawn" are each
    their own event so the full history for an encounter is reconstructed
    by reading, not by inspecting mutable state.
    """
    entry = ConsentLedgerEntry(
        encounter_id=payload.encounter_id,
        event=payload.event,
        participant_roster=json.dumps(payload.participant_roster),
        purposes=json.dumps(payload.purposes),
        script_language=payload.script_language,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return ConsentEntryOut.model_validate(entry)
