"""Capturing and reporting the pilot's success metrics (Phase 6).

The checklist's heads-up is the reason this phase exists at all:

> "The roadmap's stated mitigation for skipping the vendor bake-off is
> 'watch the edit-burden metric closely from day one of internal alpha.'
> That mitigation only exists if the metric is instrumented *before* alpha.
> If it isn't, the accepted risk quietly becomes an unmonitored risk — the
> worst outcome available, because you'll have neither pre-validation nor
> early detection."

That risk is now larger than when it was written, not smaller: decision
0035 swapped the note generator to a different vendor entirely, again
without a bake-off, and again on the strength of watching this metric. So
`capture_note_quality` runs at the one moment the data is final.

## Capture never fails a signature

`capture_note_quality` swallows its own errors. That is a deliberate
inversion of this codebase's usual fail-loudly instinct, and the reasoning
is specific: signing is the legal act that makes a doctor accountable for a
clinical note (P0-5), and a measurement is an observer of it. A bug in a
similarity ratio must never be able to prevent a doctor from signing, or
worse, roll one back. The metric is recomputable from the note and its
revisions; a refused signature in a consultation room is not recoverable at
all. A capture failure is logged and counted, and `pilot_report` reports
coverage so silent loss is visible rather than assumed away.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.encounter import Encounter
from app.models.note import Note, NoteStatus
from app.models.pilot import EncounterRating, NoteQualityMetric
from app.services.edit_burden import DEFINITION_VERSION, compute_note_burden

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for `DateTime(timezone=True)`.

    Subtracting a naive from an aware raises, which would turn every
    duration on the test path into a capture failure — the exact class of
    error the swallow above would then hide. Normalising here keeps the
    failure from ever happening rather than relying on being forgiven.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def capture_note_quality(db: Session, note: Note) -> NoteQualityMetric | None:
    """Freeze the edit-burden measurement for a just-signed note.

    Idempotent on `(note_id, definition_version)`: a retried or replayed
    signing transition updates the existing row rather than violating the
    unique constraint or double-counting the note in the ≥70% denominator.
    """
    try:
        burden = compute_note_burden(db, note)
        encounter = db.get(Encounter, note.encounter_id)

        signed_at = _aware(note.signed_at) or _utcnow()
        encounter_started = _aware(encounter.created_at) if encounter else None
        note_generated = _aware(note.created_at)

        total_seconds = int((signed_at - encounter_started).total_seconds()) if encounter_started else None
        review_seconds = int((signed_at - note_generated).total_seconds()) if note_generated else None

        row = (
            db.query(NoteQualityMetric)
            .filter(
                NoteQualityMetric.note_id == note.id,
                NoteQualityMetric.definition_version == burden.definition_version,
            )
            .one_or_none()
        )
        if row is None:
            row = NoteQualityMetric(note_id=note.id, definition_version=burden.definition_version)

        row.minor_only = burden.minor_only
        row.mean_similarity = burden.mean_similarity
        row.safety_flagged_sections = ",".join(burden.safety_flagged_sections)
        row.per_section_json = json.dumps(
            {
                name: {
                    "similarity": s.similarity,
                    "is_minor": s.is_minor,
                    "edited": s.edited,
                    "safety_flags": s.safety_flags,
                }
                for name, s in burden.sections.items()
            }
        )
        row.reconstruction_ambiguous = burden.any_ambiguous
        # Negative durations are impossible in wall-clock terms but trivially
        # producible by a clock adjustment. Stored as None rather than as a
        # negative that would drag a median somewhere impossible.
        row.total_seconds = total_seconds if total_seconds is not None and total_seconds >= 0 else None
        row.review_seconds = review_seconds if review_seconds is not None and review_seconds >= 0 else None
        row.signed_at = signed_at
        row.signed_by_clinician_id = note.signed_by_clinician_id

        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except Exception:  # noqa: BLE001 - see the module docstring: never block a signature
        logger.warning("Could not capture quality metrics for note %s", note.id, exc_info=True)
        db.rollback()
        return None


# --- the pilot report -----------------------------------------------------


@dataclass(frozen=True)
class EditBurdenSummary:
    definition_version: str
    signed_notes: int
    measured_notes: int
    minor_only: int
    #: The PRD's headline. None when nothing has been measured yet — 0.0
    #: would read as "we are failing badly" rather than "we have no data".
    minor_only_rate: float | None
    median_similarity: float | None
    safety_flagged_notes: int
    ambiguous_reconstructions: int
    #: measured/signed. Below 1.0 means capture is losing notes, and the
    #: headline rate is computed over a biased sample.
    coverage: float | None


@dataclass(frozen=True)
class DocumentationTimeSummary:
    median_total_seconds: int | None
    median_review_seconds: int | None
    sample_size: int


@dataclass(frozen=True)
class RatingSummary:
    count: int
    mean_stars: float | None
    #: How many signed encounters got a rating at all. A 5.0 mean over two
    #: ratings from forty consultations is not a satisfaction score.
    response_rate: float | None


@dataclass(frozen=True)
class ClinicianUsage:
    clinician_id: str
    encounters: int
    weeks_active: int
    last_encounter_at: datetime | None


@dataclass(frozen=True)
class FilingSummary:
    """P0-6's "correctly filed" rate, and an honest statement of its limit.

    What the system can observe is *rejected* filings — the 409s raised when
    a client confirmed a patient who does not match the encounter. What it
    cannot observe is a note filed against the wrong patient that the doctor
    confirmed anyway, because at that point every check agrees. So this is a
    measure of **caught** errors, and the true correctly-filed rate needs the
    weekly manual review below. Reporting it as if it were the real thing
    would be the most flattering possible reading of the data.
    """

    signed_notes: int
    linked_to_patient: int
    unlinked: int


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def edit_burden_summary(db: Session, *, definition_version: str = DEFINITION_VERSION) -> EditBurdenSummary:
    signed_notes = db.query(func.count(Note.id)).filter(Note.status == NoteStatus.SIGNED).scalar() or 0
    rows = db.query(NoteQualityMetric).filter(NoteQualityMetric.definition_version == definition_version).all()

    measured = len(rows)
    minor = sum(1 for r in rows if r.minor_only)
    return EditBurdenSummary(
        definition_version=definition_version,
        signed_notes=signed_notes,
        measured_notes=measured,
        minor_only=minor,
        minor_only_rate=round(minor / measured, 4) if measured else None,
        median_similarity=_median([r.mean_similarity for r in rows]),
        safety_flagged_notes=sum(1 for r in rows if r.safety_flagged_sections),
        ambiguous_reconstructions=sum(1 for r in rows if r.reconstruction_ambiguous),
        coverage=round(measured / signed_notes, 4) if signed_notes else None,
    )


def documentation_time_summary(db: Session) -> DocumentationTimeSummary:
    rows = db.query(NoteQualityMetric.total_seconds, NoteQualityMetric.review_seconds).all()
    totals = [float(r[0]) for r in rows if r[0] is not None]
    reviews = [float(r[1]) for r in rows if r[1] is not None]
    median_total = _median(totals)
    median_review = _median(reviews)
    return DocumentationTimeSummary(
        median_total_seconds=int(median_total) if median_total is not None else None,
        median_review_seconds=int(median_review) if median_review is not None else None,
        sample_size=len(rows),
    )


def rating_summary(db: Session) -> RatingSummary:
    rows = db.query(EncounterRating.stars).all()
    stars = [float(r[0]) for r in rows]
    signed = db.query(func.count(Note.id)).filter(Note.status == NoteStatus.SIGNED).scalar() or 0
    return RatingSummary(
        count=len(stars),
        mean_stars=round(sum(stars) / len(stars), 2) if stars else None,
        response_rate=round(len(stars) / signed, 4) if signed else None,
    )


def clinician_usage(db: Session, *, since_days: int = 28) -> list[ClinicianUsage]:
    """Voluntary use: is this doctor still reaching for it unprompted?

    Counts encounters *created* rather than notes signed, because the
    question is whether the doctor chose to use the product at all. A
    recording started and abandoned still answers it; a note signed weeks
    later does not, and would credit the wrong week.

    `weeks_active` is the count of distinct ISO weeks with at least one
    encounter. That is the shape the question needs — "still using it in
    week 4" is about spread, not volume, and one 40-encounter day followed
    by silence is exactly the pattern a total would hide.
    """
    cutoff = _utcnow() - timedelta(days=since_days)
    rows = db.query(Encounter.clinician_id, Encounter.created_at).filter(Encounter.created_at >= cutoff).all()

    by_clinician: dict[str, list[datetime]] = {}
    for clinician_id, created_at in rows:
        when = _aware(created_at)
        if when is not None:
            by_clinician.setdefault(clinician_id, []).append(when)

    usage = [
        ClinicianUsage(
            clinician_id=clinician_id,
            encounters=len(times),
            weeks_active=len({(t.isocalendar().year, t.isocalendar().week) for t in times}),
            last_encounter_at=max(times),
        )
        for clinician_id, times in by_clinician.items()
    ]
    return sorted(usage, key=lambda u: (-u.encounters, u.clinician_id))


def filing_summary(db: Session) -> FilingSummary:
    signed = (
        db.query(func.count(Note.id), func.count(Encounter.patient_id))
        .join(Encounter, Encounter.id == Note.encounter_id)
        .filter(Note.status == NoteStatus.SIGNED)
        .one()
    )
    total, linked = int(signed[0] or 0), int(signed[1] or 0)
    return FilingSummary(signed_notes=total, linked_to_patient=linked, unlinked=total - linked)


def review_sample(db: Session, *, sample_size: int = 10, week_offset: int = 0) -> list[str]:
    """The weekly manual-review sample for unsafe-acceptance rate.

    Two properties matter more than the sampling being clever:

    * **Deterministic.** The same week returns the same note ids on every
      call, so two reviewers examine the same notes and a reviewer can stop
      and resume. A `random.sample` would silently give each reviewer a
      different set and make disagreement uninterpretable.
    * **Biased toward the notes worth reading.** Safety-flagged notes are
      offered first. Uniform sampling of a mostly-fine population spends the
      reviewer's scarce attention on notes nobody needed to check, and the
      point of this workflow is catching an unsafe acceptance, not
      estimating a mean.

    Returns note ids only. The reviewer opens each in the normal UI, so the
    read is audited (Phase 4.2) like any other — a sampling endpoint that
    returned note text would be an unaudited bulk PHI export wearing a
    different hat.
    """
    now = _utcnow()
    end = now - timedelta(weeks=week_offset)
    start = end - timedelta(weeks=1)

    rows = (
        db.query(NoteQualityMetric.note_id, NoteQualityMetric.safety_flagged_sections)
        .filter(NoteQualityMetric.signed_at >= start, NoteQualityMetric.signed_at < end)
        .order_by(NoteQualityMetric.note_id)
        .all()
    )
    flagged = [r[0] for r in rows if r[1]]
    unflagged = [r[0] for r in rows if not r[1]]
    return (flagged + unflagged)[:sample_size]
