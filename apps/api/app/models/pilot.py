"""Pilot instrumentation (Phase 6) — the tables that hold what the PRD
promised to measure.

Two rows, two very different lifetimes, and the split is deliberate:

* `NoteQualityMetric` is **computed once, at signing, and frozen**. It is
  derived data — everything in it could in principle be recomputed from
  `NoteRevision` — but recomputation is exactly what must not happen
  silently. Retention deletes revisions (Phase 4.4) while the signed note
  is a permanent record, so a metric that depended on live revisions would
  quietly become uncomputable partway through the pilot. Freezing it also
  makes `definition_version` meaningful: the number and the rules that
  produced it travel together.

* `EncounterRating` is a doctor's own five-star judgement, which nothing
  can derive. It is the only signal here that is not computed from the
  data the product already holds.

**Neither table stores PHI.** That is a design constraint, not an
accident. Metrics are similarity ratios, booleans, durations and star
counts; the safety flags name *categories* ("numeric value changed"), never
the text that changed. This is what lets a pilot report be read by someone
without clinical access, and what keeps these rows outside the retention
purge that must eventually delete the notes they describe — a metric that
had to be deleted with its note could not answer whether the pilot passed.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import EncryptedString
from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class NoteQualityMetric(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One row per (note, definition version) — the edit-burden measurement
    behind the PRD's "≥70% of signed notes require only minor edits".

    Keyed by note **and** definition version rather than note alone: if the
    definition changes mid-pilot, re-scoring writes new rows beside the old
    ones instead of overwriting them, so a report can say which definition
    it is quoting and a mixed-version report is detectable rather than
    merely wrong. See app/services/edit_burden.py.
    """

    __tablename__ = "note_quality_metrics"
    __table_args__ = (
        UniqueConstraint("note_id", "definition_version", name="uq_note_quality_note_definition"),
        # A similarity ratio outside [0, 1] means the computation is broken,
        # and a broken metric that still writes rows is worse than one that
        # fails: it produces a plausible pilot verdict from nonsense.
        CheckConstraint(
            "mean_similarity >= 0.0 AND mean_similarity <= 1.0",
            name="ck_note_quality_similarity_range",
        ),
    )

    note_id: Mapped[str] = mapped_column(String(36), ForeignKey("notes.id"), nullable=False, index=True)
    definition_version: Mapped[str] = mapped_column(String(32), nullable=False)

    #: The headline input to the ≥70% target: every section minor.
    minor_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    mean_similarity: Mapped[float] = mapped_column(Float, nullable=False)
    #: Sections whose edit tripped a clinical-safety rule (a changed dose, a
    #: flipped negation). Comma-separated section names — never the text.
    #: Non-empty here is why a note can be a one-character edit and still not
    #: count as minor.
    safety_flagged_sections: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: JSON: {section: {"similarity": float, "is_minor": bool, "edited": bool,
    #: "safety_flags": [str]}}. Kept for distribution analysis, so the
    #: threshold can be recalibrated from real data without re-deriving
    #: anything from revisions that retention may since have deleted.
    per_section_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    #: True when the revision chain could not be rooted (an edit-then-revert).
    #: Recorded rather than hidden: a pilot report should be able to say how
    #: many of its inputs were ambiguous.
    reconstruction_ambiguous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- documentation time -------------------------------------------
    #: Signing minus the encounter's creation: the whole interaction, which
    #: is what compares to the week-0 paper baseline.
    total_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Signing minus note generation: the part the product actually
    #: controls. Kept separate because a doctor who leaves a note open over
    #: lunch inflates the first number and not the second, and conflating
    #: them makes the headline figure unusable.
    review_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_by_clinician_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clinicians.id"), nullable=True)


class EncounterRating(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The post-encounter five-star prompt (PRD success metrics).

    One rating per encounter, enforced in the schema. A doctor who rates the
    same encounter twice is changing their mind, not adding a data point,
    and averaging both would weight them double.

    `comment` is nullable and free text, which makes it the one field here
    that *could* carry PHI — a doctor is perfectly capable of typing a
    patient's name into a feedback box. It is therefore encrypted at rest
    like any other free-text clinical field, and excluded from the pilot
    report endpoint, which returns only the numbers.
    """

    __tablename__ = "encounter_ratings"
    __table_args__ = (
        UniqueConstraint("encounter_id", name="uq_encounter_rating_encounter"),
        CheckConstraint("stars >= 1 AND stars <= 5", name="ck_encounter_rating_stars"),
    )

    encounter_id: Mapped[str] = mapped_column(String(36), ForeignKey("encounters.id"), nullable=False, index=True)
    clinician_id: Mapped[str] = mapped_column(String(36), ForeignKey("clinicians.id"), nullable=False, index=True)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(EncryptedString(2048), nullable=True)
