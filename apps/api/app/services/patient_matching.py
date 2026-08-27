"""P0-6: "Starting a session accepts a typed or dictated patient name and
fuzzy-matches against the existing directory (exact match links silently;
near match requires one-tap confirmation; no match creates a new record
with name + birthdate)." and "Deduplication uses name + birthdate together,
not name alone."

Uses stdlib difflib rather than pulling in rapidfuzz/thefuzz — the match
set per birthdate is small (patients sharing an exact birthdate), so
O(n) SequenceMatcher against that filtered set is plenty fast, and it
keeps the dependency list in requirements.txt short.
"""

from __future__ import annotations

from datetime import date
from difflib import SequenceMatcher

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import EncryptedString
from app.models.patient import Patient
from app.schemas.patient import PatientMatchResult, PatientOut, PatientSearchHit

NEAR_MATCH_THRESHOLD = 0.82


def _normalize(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def match_patient(db: Session, name: str, birthdate: date) -> PatientMatchResult:
    """Birthdate narrows the candidate set first (the "+birthdate" half of
    dedup), then name similarity decides exact vs. near vs. none within
    that set — never matches purely on name.
    """

    candidates = db.query(Patient).filter(Patient.birthdate == birthdate).all()

    exact = next((p for p in candidates if _normalize(p.full_name) == _normalize(name)), None)
    if exact:
        return PatientMatchResult(match_type="exact", patient=PatientOut.model_validate(exact))

    near = [p for p in candidates if _similarity(p.full_name, name) >= NEAR_MATCH_THRESHOLD]
    if near:
        return PatientMatchResult(
            match_type="near",
            candidates=[PatientOut.model_validate(p) for p in near],
        )

    return PatientMatchResult(match_type="none")


def create_patient(db: Session, name: str, birthdate: date) -> Patient:
    patient = Patient(full_name=name, birthdate=birthdate)
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


# --- Phase 2.5: name-first fuzzy search over ENCRYPTED names --------------
#
# P0-6's entry point is a name: "Starting a session accepts a typed or
# dictated patient name and fuzzy-matches against the existing directory."
# `match_patient` above cannot serve that — it needs a birthdate first.
#
# The checklist's 🧠 proposes a blind index (an HMAC of the name) as the way
# to search encrypted names. That solves the wrong half of this problem: an
# HMAC supports EXACT equality only, and P0-6 requires FUZZY matching. You
# cannot compute a similarity ratio against a hash.
#
# So names are decrypted and compared in Python. That was measured rather
# than assumed, because the naive version is far too slow — at 5,000
# patients, `db.query(Patient).all()` plus difflib over every row took
# ~2.1 seconds. The breakdown showed where it actually goes:
#
#     raw SELECT of ciphertext ................    7.7 ms
#     + decrypting every value ...............   118   ms
#     full ORM query instead of raw SELECT ...   348   ms   <-- biggest cost
#     difflib over all names .................   183   ms
#     token prefilter, then difflib ..........    68   ms   <-- 2.7x better
#
# Decryption is NOT the bottleneck; ORM hydration and unfiltered difflib
# are. Hence the two choices below: a raw SELECT of just the three columns
# needed (no ORM objects), and a cheap token prefilter before any
# similarity work. See docs/decisions/0029 for the full reasoning and the
# scale ceiling this leaves.

SEARCH_MATCH_THRESHOLD = 0.55
DEFAULT_SEARCH_LIMIT = 10


def _tokens(name: str) -> set[str]:
    return set(_normalize(name).split())


def _name_column_type() -> EncryptedString:
    """The column's own type decorator, so search decrypts with exactly the
    same code path as ORM attribute access. Constructing a second
    EncryptedString here would risk the two drifting apart.
    """
    return Patient.__table__.c.full_name.type  # type: ignore[return-value]


def search_patients_by_name(
    db: Session,
    query: str,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[PatientSearchHit]:
    """Ranked fuzzy search by name alone (P0-6).

    Deliberately does NOT take a birthdate: this is the search that happens
    before the doctor knows which patient they mean. Birthdate still governs
    *deduplication* — `match_patient` is what runs once a candidate is
    chosen or a new record is created, so "dedup uses name + birthdate
    together, not name alone" continues to hold.
    """
    normalized = _normalize(query)
    if not normalized:
        return []

    # Raw SELECT of only what is needed. The ORM path costs ~3x more for
    # rows that are immediately discarded — see the measurements above.
    rows = db.execute(text("SELECT id, full_name, birthdate FROM patients")).all()
    coltype = _name_column_type()

    query_tokens = _tokens(query)
    hits: list[PatientSearchHit] = []

    for row in rows:
        name = coltype.process_result_value(row[1], dialect=None)
        if name is None:
            continue

        name_tokens = _tokens(name)
        # Prefilter: a shared whole token, or any token that prefix-matches.
        # Cheap set work in place of an expensive SequenceMatcher call, and
        # it is what turns 183 ms of similarity work into 68 ms. A candidate
        # sharing no token and no prefix is not a plausible typo of the
        # query, so skipping it costs no real recall.
        shares_token = bool(query_tokens & name_tokens)
        shares_prefix = any(
            nt.startswith(qt) or qt.startswith(nt)
            for qt in query_tokens
            for nt in name_tokens
            if len(qt) >= 3 and len(nt) >= 3
        )
        if not (shares_token or shares_prefix):
            continue

        score = _similarity(name, query)
        # A shared token is itself evidence, so a partial-name query ("Maria
        # Cruz" against "Maria Santos Dela Cruz") is not thrown away just
        # because the full strings differ.
        if score < SEARCH_MATCH_THRESHOLD and not shares_token:
            continue

        birthdate = row[2]
        if isinstance(birthdate, str):
            # SQLite hands back a string where Postgres gives a date.
            birthdate = date.fromisoformat(birthdate)

        hits.append(
            PatientSearchHit(
                id=row[0],
                full_name=name,
                birthdate=birthdate,
                score=round(score, 4),
                match_type="exact" if _normalize(name) == normalized else "near",
            )
        )

    # Exact matches first, then by score. Ties broken by name so the order
    # is stable across requests rather than dependent on row order.
    hits.sort(key=lambda h: (h.match_type != "exact", -h.score, h.full_name))
    return hits[:limit]


def previous_signed_note(db: Session, patient_id: str, exclude_encounter_id: str | None = None):
    """The patient's most recent SIGNED note (P0-5: "Show the prior visit's
    assessment and plan for longitudinal context").

    Only signed notes count. A draft or filed-but-unsigned note from a
    previous visit is not yet part of the record a doctor should be reasoning
    from — surfacing one as "the last visit" would present an unreviewed AI
    draft as established history, which is the opposite of what the
    signing ceremony is for.
    """
    from app.models.encounter import Encounter
    from app.models.note import Note, NoteStatus

    query = (
        db.query(Note)
        .join(Encounter, Note.encounter_id == Encounter.id)
        .filter(Encounter.patient_id == patient_id)
        .filter(Note.status == NoteStatus.SIGNED)
    )
    if exclude_encounter_id:
        query = query.filter(Note.encounter_id != exclude_encounter_id)
    return query.order_by(Note.signed_at.desc()).first()
