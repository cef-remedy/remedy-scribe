"""Pilot instrumentation response shapes (Phase 6).

Every field here is a count, a ratio, a duration or an id. No note text, no
patient identifier, no rating comment — the report is deliberately readable
by someone without clinical access, because the people who need to know
whether the pilot is passing are not always the people cleared to read
consultations.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class RatingCreate(BaseModel):
    """The post-encounter five-star prompt."""

    stars: int = Field(ge=1, le=5)
    #: Optional free text. Encrypted at rest (it is the one field a doctor
    #: could type a patient name into) and never returned by the report.
    comment: str | None = Field(default=None, max_length=2000)


class RatingOut(BaseModel):
    encounter_id: str
    stars: int
    created_at: datetime

    model_config = {"from_attributes": True}


class EditBurdenOut(BaseModel):
    """The PRD's headline: "≥70% of signed notes require only minor edits"."""

    definition_version: str
    signed_notes: int
    measured_notes: int
    minor_only: int
    #: None rather than 0.0 when nothing is measured yet — "no data" and
    #: "failing badly" must not render identically on a dashboard.
    minor_only_rate: float | None
    median_similarity: float | None
    #: Notes where a small edit changed a dose, a number or a negation.
    #: These are never counted as minor regardless of how few characters
    #: moved, and they are the ones the weekly review should read first.
    safety_flagged_notes: int
    ambiguous_reconstructions: int
    #: measured/signed. Below 1.0 means capture lost notes and the headline
    #: rate is computed over a biased sample.
    coverage: float | None

    model_config = {"from_attributes": True}


class DocumentationTimeOut(BaseModel):
    #: Encounter creation to signature — comparable to the week-0 paper
    #: baseline.
    median_total_seconds: int | None
    #: Note generation to signature — the part the product controls.
    median_review_seconds: int | None
    sample_size: int

    model_config = {"from_attributes": True}


class RatingSummaryOut(BaseModel):
    count: int
    mean_stars: float | None
    #: Ratings per signed note. A 5.0 mean from two responses out of forty
    #: consultations is not a satisfaction score.
    response_rate: float | None

    model_config = {"from_attributes": True}


class ClinicianUsageOut(BaseModel):
    clinician_id: str
    encounters: int
    #: Distinct ISO weeks with at least one encounter. "Still using it in
    #: week 4" is a question about spread, not volume.
    weeks_active: int
    last_encounter_at: datetime | None

    model_config = {"from_attributes": True}


class FilingSummaryOut(BaseModel):
    """Caught filing errors, not the true correctly-filed rate.

    The system sees rejected filings (a confirmed patient that does not
    match the encounter). It cannot see a note filed to the wrong patient
    that the doctor confirmed anyway — at that point every check agrees.
    The real rate needs the weekly manual review.
    """

    signed_notes: int
    linked_to_patient: int
    unlinked: int

    model_config = {"from_attributes": True}


class PilotReportOut(BaseModel):
    generated_at: datetime
    edit_burden: EditBurdenOut
    documentation_time: DocumentationTimeOut
    ratings: RatingSummaryOut
    filing: FilingSummaryOut
    usage: list[ClinicianUsageOut]


class ReviewSampleOut(BaseModel):
    """Note ids for the weekly unsafe-acceptance review.

    Ids only, on purpose: the reviewer opens each note through the normal
    UI so the read is audited like any other (Phase 4.2). An endpoint that
    returned the text itself would be an unaudited bulk PHI export.
    """

    week_offset: int
    sample_size: int
    note_ids: list[str]
