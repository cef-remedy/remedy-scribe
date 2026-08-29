"""Pilot instrumentation endpoints (Phase 6).

Two audiences with genuinely different access, which is why the RBAC below
is not uniform:

* **A doctor rates their own encounter.** `POST /encounters/{id}/rating` is
  doctor-only and scoped to the encounter they ran.
* **The pilot report is for whoever decides go/no-go**, which is compliance
  and admin as much as clinicians. It contains no PHI by construction, so
  opening it to those roles discloses nothing a pilot decision-maker should
  not see — and *not* opening it would mean the person answering "did this
  pass?" has to ask a doctor to read them the numbers.

The review sample is the exception in the other direction: it returns note
ids, and following one means reading a consultation. Doctor and compliance
only, and the read itself is audited when the reviewer opens the note
through the normal route.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.clinician import Clinician
from app.models.encounter import Encounter
from app.models.pilot import EncounterRating
from app.schemas.pilot import (
    ClinicianUsageOut,
    DocumentationTimeOut,
    EditBurdenOut,
    FilingSummaryOut,
    PilotReportOut,
    RatingCreate,
    RatingOut,
    RatingSummaryOut,
    ReviewSampleOut,
)
from app.services import audit
from app.services.pilot_metrics import (
    clinician_usage,
    documentation_time_summary,
    edit_burden_summary,
    filing_summary,
    rating_summary,
    review_sample,
)

router = APIRouter(prefix="/pilot", tags=["pilot"])


@router.post(
    "/encounters/{encounter_id}/rating",
    response_model=RatingOut,
    status_code=status.HTTP_201_CREATED,
)
def rate_encounter(
    encounter_id: str,
    payload: RatingCreate,
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor")),
) -> RatingOut:
    """The post-encounter five-star prompt.

    An existing rating is **updated, not duplicated**: a doctor rating the
    same encounter twice has changed their mind, and counting both would
    weight one consultation double in the mean. The unique constraint on
    `encounter_id` enforces that in the schema as well, so a concurrent
    double-submit cannot get around it.
    """
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Encounter not found")

    rating = db.query(EncounterRating).filter(EncounterRating.encounter_id == encounter_id).one_or_none()
    if rating is None:
        rating = EncounterRating(encounter_id=encounter_id, clinician_id=clinician.id)
    rating.stars = payload.stars
    rating.comment = payload.comment
    db.add(rating)
    db.commit()
    db.refresh(rating)

    # Audited as a write against the encounter (Phase 4.2's rule: log every
    # capability over PHI). The stars and the comment are not recorded --
    # the comment is the one field here a patient name could reach, and an
    # audit row outlives what it describes.
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="encounter.rating.submit",
        entity_type="encounter",
        entity_id=encounter_id,
    )
    return RatingOut(encounter_id=rating.encounter_id, stars=rating.stars, created_at=rating.created_at)


@router.get("/report", response_model=PilotReportOut)
def pilot_report(
    usage_days: int = Query(default=28, ge=1, le=365),
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor", "compliance", "admin")),
) -> PilotReportOut:
    """Everything the PRD promised to measure, in one read.

    Contains no PHI: counts, ratios, durations and clinician ids only.
    Audited anyway -- knowing who is watching the pilot's numbers is
    ordinary accountability, and the row costs nothing.
    """
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="pilot.report.read",
        entity_type="pilot",
        entity_id="*",
    )
    return PilotReportOut(
        generated_at=datetime.now(timezone.utc),
        edit_burden=EditBurdenOut.model_validate(edit_burden_summary(db)),
        documentation_time=DocumentationTimeOut.model_validate(documentation_time_summary(db)),
        ratings=RatingSummaryOut.model_validate(rating_summary(db)),
        filing=FilingSummaryOut.model_validate(filing_summary(db)),
        usage=[ClinicianUsageOut.model_validate(u) for u in clinician_usage(db, since_days=usage_days)],
    )


@router.get("/review-sample", response_model=ReviewSampleOut)
def weekly_review_sample(
    sample_size: int = Query(default=10, ge=1, le=100),
    week_offset: int = Query(default=0, ge=0, le=52),
    db: Session = Depends(get_db),
    clinician: Clinician = Depends(require_role("doctor", "compliance")),
) -> ReviewSampleOut:
    """The weekly manual-review sample for unsafe-acceptance rate.

    Deterministic for a given week, so two reviewers read the same notes and
    a reviewer can stop and resume; safety-flagged notes come first, because
    the point is catching an unsafe acceptance rather than estimating a mean
    over a mostly-fine population.
    """
    note_ids = review_sample(db, sample_size=sample_size, week_offset=week_offset)
    audit.record(
        db,
        actor_clinician_id=clinician.id,
        action="pilot.review_sample.read",
        entity_type="pilot",
        entity_id="*",
    )
    return ReviewSampleOut(week_offset=week_offset, sample_size=sample_size, note_ids=note_ids)
