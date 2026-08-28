import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_clinician, get_db, require_role
from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.services import audit
from app.services.consent import current_consent_state
from app.tasks.retention import purge_withdrawn_encounter
from app.schemas.consent import (
    ConsentEntryCreate,
    ConsentEntryWithOutcomeOut,
    ConsentStateOut,
    WithdrawalOutcomeOut,
)

router = APIRouter(prefix="/consent", tags=["consent"])


@router.post("", response_model=ConsentEntryWithOutcomeOut, status_code=201)
def record_consent_event(
    payload: ConsentEntryCreate,
    db: Session = Depends(get_db),
    # RBAC (0.2): consent is captured by the clinician running the
    # recording — not a compliance/admin action, so it's scoped to
    # "doctor" like the rest of the recording workflow.
    clinician: Clinician = Depends(require_role("doctor")),
) -> ConsentEntryWithOutcomeOut:
    """P0-1: appends one row to the immutable consent ledger. Never
    updates a prior row — "given", "declined", and "withdrawn" are each
    their own event so the full history for an encounter is reconstructed
    by reading, not by inspecting mutable state.

    Phase 2.3: a "withdrawn" event additionally triggers the audio-deletion
    path (P0-1: "processing stops and the associated audio is queued for
    deletion without undue delay"). The ledger entry is committed *first*
    and deliberately: it is the legal record and must survive even if the
    deletion below fails. The response reports what actually happened so the
    UI can tell the doctor the truth rather than an optimistic guess.
    """
    entry = ConsentLedgerEntry(
        encounter_id=payload.encounter_id,
        event=payload.event,
        participant_roster=json.dumps(payload.participant_roster),
        purposes=json.dumps(payload.purposes),
        script_language=payload.script_language,
    )
    db.add(entry)
    # Committed before any deletion is attempted: the ledger is the legal
    # record, and it must not be rolled back by a storage failure.
    db.commit()
    db.refresh(entry)

    # Phase 4.2: the consent ledger is the legal record of *what* was
    # consented to, and it deliberately carries no clinician id — an
    # encounter's consent history is a statement about the patient, not
    # about who typed it. So the audit trail is the only place that records
    # **who** captured a consent event, and for a withdrawal (which
    # triggers deletion of the recording) that is exactly the fact an
    # investigation needs. Recorded against the encounter, since that is
    # what a reviewer looks a consent event up by; the ledger entry id goes
    # in the diff so the two records can be tied together.
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action=f"consent.record.{payload.event.value}",
        entity_type="encounter",
        entity_id=payload.encounter_id,
        diff={"consent_ledger_entry_id": entry.id},
    )

    result = ConsentEntryWithOutcomeOut.model_validate(entry)
    if payload.event == "withdrawn":
        # Phase 4.4: purge_withdrawn_encounter *wraps* handle_withdrawal
        # rather than replacing it — P0-1's audio deletion keeps its single
        # definition and the derived PHI (transcript, note revisions) is
        # deleted in the same pass. Without this the derived rows waited on
        # the hourly sweep, which is a strange thing to make a patient who
        # just withdrew consent wait for. Same return type.
        outcome = purge_withdrawn_encounter(db, payload.encounter_id, actor_clinician_id=clinician.id)
        result.withdrawal = WithdrawalOutcomeOut(
            pipeline_will_stop=outcome.pipeline_will_stop,
            audio_deleted=outcome.audio_deleted,
            nothing_to_delete=outcome.nothing_to_delete,
            retention_expired_immediately=outcome.retention_expired_immediately,
        )
    return result


@router.get("/{encounter_id}", response_model=ConsentStateOut)
def read_consent_state(
    encounter_id: str,
    db: Session = Depends(get_db),
    # Any authenticated clinician, not doctor-only: this is a *read* of
    # whether recording is permitted, and the same reasoning as note reads
    # (decision 0004) applies — restricting it buys nothing while breaking
    # a compliance officer's ability to check consent state.
    clinician: Clinician = Depends(get_current_clinician),
) -> ConsentStateOut:
    """The client-side half of the P0-1 gate. The server already refuses
    to finalize an upload or transcribe without consent (Phase 0.1); this
    lets the app refuse to *capture* in the first place, which is what
    P0-1 actually asks for — "before anything is captured".
    """
    state = current_consent_state(db, encounter_id)
    # Phase 4.2. The thinnest disclosure in the API — it answers one
    # yes/no about an encounter — and logged anyway, because the alternative
    # is a per-route judgment call about what counts as "enough" PHI to be
    # worth an audit row, and that judgment is exactly what leaves gaps.
    # Not coalesced: the client calls this once per consent gate, not on a
    # timer (apps/web/src/lib/consent.ts).
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="consent.read",
        entity_type="encounter",
        entity_id=encounter_id,
    )
    return ConsentStateOut(
        encounter_id=state.encounter_id,
        can_record=state.is_given,
        latest_event=state.latest_event,
        script_language=state.script_language,
        entry_count=state.entry_count,
    )
