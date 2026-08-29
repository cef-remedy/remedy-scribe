"""Phase 6: pilot instrumentation.

The bulk of this file tests one definition — what counts as a "minor edit" —
because that definition decides whether the pilot passes, and every obvious
way to write it is wrong in a way that flatters the product:

* a character-distance threshold calls `5mg` -> `50mg` minor;
* summing per-revision distances scores a typed-then-deleted word as heavy
  editing when the net change is nil;
* ordering revisions by timestamp reintroduces the non-determinism decision
  0027 already recorded.

Each of those has a test named after the failure it prevents.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.security import create_access_token
from app.models.clinician import Clinician
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note, NoteRevision, NoteStatus
from app.models.patient import Patient
from app.models.pilot import EncounterRating, NoteQualityMetric
from app.services.edit_burden import (
    DEFINITION_VERSION,
    MINOR_SIMILARITY_THRESHOLD,
    compute_note_burden,
    generated_text,
)
from app.services.pilot_metrics import (
    capture_note_quality,
    clinician_usage,
    documentation_time_summary,
    edit_burden_summary,
    filing_summary,
    rating_summary,
    review_sample,
)

_DRAFT = "Patient appears to have an acute febrile illness. Advised paracetamol 500mg every six hours."


def _doctor(db, email: str = "doc@example.com") -> Clinician:
    c = Clinician(email=email, full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _auth(c: Clinician) -> dict:
    return {"Authorization": f"Bearer {create_access_token(subject=c.id, extra_claims={'role': c.role})}"}


def _encounter(db, clinician, *, key="idem-pilot-1", patient=True, created_at=None) -> Encounter:
    patient_id = None
    if patient:
        p = Patient(full_name="Maria Santos", birthdate=date(1990, 1, 1))
        db.add(p)
        db.commit()
        db.refresh(p)
        patient_id = p.id
    e = Encounter(
        patient_id=patient_id,
        clinician_id=clinician.id,
        upload_idempotency_key=key,
        pipeline_status=EncounterPipelineStatus.NOTE_GENERATED,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    if created_at is not None:
        e.created_at = created_at
        db.add(e)
        db.commit()
        db.refresh(e)
    return e


def _note(db, encounter, *, assessment=_DRAFT, plan="", status=NoteStatus.GENERATED) -> Note:
    n = Note(
        encounter_id=encounter.id,
        status=status,
        assessment=assessment,
        plan=plan,
        subjective="",
        objective="",
        source_spans="{}",
        note_generator_provider="groq",
        prompt_version="groq-v1",
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _edit(db, note, clinician, section, previous, new):
    db.add(
        NoteRevision(
            note_id=note.id,
            section=section,
            previous_text=previous,
            new_text=new,
            edited_by_clinician_id=clinician.id,
        )
    )
    setattr(note, section, new)
    db.add(note)
    db.commit()
    db.refresh(note)


# --- the definition: small AND clinically inert ---------------------------


def test_an_untouched_note_is_minor(db):
    c = _doctor(db)
    note = _note(db, _encounter(db, c))

    burden = compute_note_burden(db, note)

    assert burden.minor_only is True
    assert burden.mean_similarity == 1.0
    assert burden.sections["assessment"].edited is False


def test_a_rephrase_is_minor(db):
    c = _doctor(db)
    note = _note(db, _encounter(db, c))
    _edit(
        db,
        note,
        c,
        "assessment",
        _DRAFT,
        "Patient appears to have an acute febrile illness. Advised paracetamol 500mg every 6 hours.",
    )

    burden = compute_note_burden(db, note)
    section = burden.sections["assessment"]

    assert section.edited is True
    # "six" -> "6" is a numeric token change, so this is *not* minor even
    # though it reads as cosmetic. Documented rather than special-cased:
    # erring toward flagging a quantity is the safe direction.
    assert section.safety_flags


def test_a_pure_wording_change_with_no_numbers_is_minor(db):
    draft = (
        "Patient appears to have an acute febrile illness consistent with a viral upper "
        "respiratory infection. Symptoms reported as beginning three days ago with "
        "associated cough and malaise. No red flags elicited on history today."
    )
    c = _doctor(db)
    note = _note(db, _encounter(db, c), assessment=draft)
    _edit(db, note, c, "assessment", draft, draft.replace("malaise", "general malaise"))

    section = compute_note_burden(db, note).sections["assessment"]

    assert section.safety_flags == []
    assert section.similarity >= MINOR_SIMILARITY_THRESHOLD
    assert section.is_minor is True


def test_a_changed_dose_is_never_minor_however_small_the_edit(db):
    """The failure this exists to prevent. `500mg` -> `5000mg` is a
    one-character edit and the most dangerous correction available; a
    character-distance threshold would score it as maximally minor.
    """
    c = _doctor(db)
    note = _note(db, _encounter(db, c))
    _edit(db, note, c, "assessment", _DRAFT, _DRAFT.replace("500mg", "5000mg"))

    section = compute_note_burden(db, note).sections["assessment"]

    assert section.similarity > MINOR_SIMILARITY_THRESHOLD  # tiny by distance
    assert section.is_minor is False  # and still not minor
    assert any("numeric" in f for f in section.safety_flags)
    assert compute_note_burden(db, note).minor_only is False


def test_a_changed_unit_is_caught_even_when_the_digits_are_identical(db):
    """`500mg` -> `500mcg` is a 1000x error with the same number in it."""
    c = _doctor(db)
    note = _note(db, _encounter(db, c))
    _edit(db, note, c, "assessment", _DRAFT, _DRAFT.replace("500mg", "500mcg"))

    section = compute_note_burden(db, note).sections["assessment"]

    assert section.is_minor is False
    assert any("dose unit" in f or "numeric" in f for f in section.safety_flags)


def test_a_flipped_negation_is_never_minor(db):
    """`no chest pain` -> `chest pain` is a three-character deletion and a
    different patient.
    """
    c = _doctor(db)
    draft = "Reports no chest pain on exertion."
    note = _note(db, _encounter(db, c), assessment=draft)
    _edit(db, note, c, "assessment", draft, "Reports chest pain on exertion.")

    section = compute_note_burden(db, note).sections["assessment"]

    assert section.is_minor is False
    assert any("negation" in f for f in section.safety_flags)


def test_a_filipino_negation_flip_is_caught_too(db):
    """P0-3 keeps Taglish verbatim, so the negation vocabulary has to cover
    it — otherwise a negation flip in the patient's own words reads as inert.
    """
    c = _doctor(db)
    draft = "Sabi ng pasyente walang sakit ang dibdib."
    note = _note(db, _encounter(db, c), assessment=draft)
    _edit(db, note, c, "assessment", draft, "Sabi ng pasyente may sakit ang dibdib.")

    section = compute_note_burden(db, note).sections["assessment"]

    assert section.is_minor is False
    assert any("negation" in f for f in section.safety_flags)


def test_a_full_rewrite_is_not_minor(db):
    c = _doctor(db)
    note = _note(db, _encounter(db, c))
    _edit(db, note, c, "assessment", _DRAFT, "Entirely different clinical impression written by the doctor.")

    burden = compute_note_burden(db, note)

    assert burden.sections["assessment"].similarity < MINOR_SIMILARITY_THRESHOLD
    assert burden.minor_only is False


def test_one_rewritten_section_disqualifies_the_whole_note(db):
    """A note is signed as one document. A rewritten Plan is not redeemed by
    an untouched Subjective.
    """
    c = _doctor(db)
    note = _note(db, _encounter(db, c), plan="Rest and fluids.")
    _edit(db, note, c, "plan", "Rest and fluids.", "Refer urgently to cardiology for suspected ACS.")

    burden = compute_note_burden(db, note)

    assert burden.sections["assessment"].is_minor is True
    assert burden.sections["plan"].is_minor is False
    assert burden.minor_only is False


# --- generated -> signed, not the sum of revisions ------------------------


def test_typing_and_deleting_scores_as_no_change(db):
    """Three revisions, zero net change. Summing per-revision distances
    would call this heavy editing, which is the opposite of true.
    """
    c = _doctor(db)
    note = _note(db, _encounter(db, c))
    _edit(db, note, c, "assessment", _DRAFT, _DRAFT + " Extra.")
    _edit(db, note, c, "assessment", _DRAFT + " Extra.", _DRAFT + " Extra text here.")
    _edit(db, note, c, "assessment", _DRAFT + " Extra text here.", _DRAFT)

    burden = compute_note_burden(db, note)

    assert burden.sections["assessment"].similarity == 1.0
    assert burden.minor_only is True


def test_the_generated_text_is_recovered_from_the_chain_root(db):
    c = _doctor(db)
    note = _note(db, _encounter(db, c))
    _edit(db, note, c, "assessment", _DRAFT, "second")
    _edit(db, note, c, "assessment", "second", "third")

    original, ambiguous = generated_text(db, note.id, "assessment", note.assessment)

    assert original == _DRAFT
    assert ambiguous is False


def test_chain_reconstruction_does_not_depend_on_timestamp_order(db):
    """Decision 0027 recorded that identical `created_at` values make
    revision ordering non-deterministic. The root is found structurally --
    the input that is nobody's output -- so identical timestamps are
    harmless.
    """
    c = _doctor(db)
    note = _note(db, _encounter(db, c))
    _edit(db, note, c, "assessment", _DRAFT, "second")
    _edit(db, note, c, "assessment", "second", "third")

    same = datetime.now(timezone.utc)
    for row in db.query(NoteRevision).filter(NoteRevision.note_id == note.id).all():
        row.created_at = same
        db.add(row)
    db.commit()

    original, ambiguous = generated_text(db, note.id, "assessment", note.assessment)

    assert original == _DRAFT
    assert ambiguous is False


def test_an_edit_then_revert_is_reported_ambiguous_rather_than_guessed(db):
    """A->B then B->A makes every value both an input and an output. The net
    change is nil, so the honest answer is available without resolving the
    order — but the caller is told the reconstruction was ambiguous.
    """
    c = _doctor(db)
    note = _note(db, _encounter(db, c))
    _edit(db, note, c, "assessment", _DRAFT, "changed")
    _edit(db, note, c, "assessment", "changed", _DRAFT)

    original, ambiguous = generated_text(db, note.id, "assessment", note.assessment)

    assert ambiguous is True
    assert original == _DRAFT
    assert compute_note_burden(db, note).any_ambiguous is True


# --- capture at signing ---------------------------------------------------


def test_signing_captures_a_frozen_metric(db, client):
    c = _doctor(db)
    e = _encounter(db, c)
    note = _note(db, e, status=NoteStatus.AUTHENTICATED)

    from app.services.note_lifecycle import transition

    transition(db, note, NoteStatus.SIGNED, clinician_id=c.id, prc_license_number="PRC-1")

    row = db.query(NoteQualityMetric).filter(NoteQualityMetric.note_id == note.id).one()
    assert row.definition_version == DEFINITION_VERSION
    assert row.minor_only is True
    assert row.signed_by_clinician_id == c.id
    assert json.loads(row.per_section_json)["assessment"]["is_minor"] is True


def test_capture_never_blocks_a_signature(db, monkeypatch):
    """A bug in a similarity ratio must not refuse a doctor's signature. The
    measurement is recomputable; a refused signature in a consultation room
    is not.
    """
    c = _doctor(db)
    note = _note(db, _encounter(db, c), status=NoteStatus.AUTHENTICATED)

    def _boom(*a, **kw):
        raise RuntimeError("metric computation is broken")

    # Patched where it is *used*, not where it is defined. `pilot_metrics`
    # does `from app.services.edit_burden import compute_note_burden`, which
    # binds the function object at import — so patching the source module
    # leaves the imported reference untouched and the real function runs.
    # This is the same trap Phase 1.5 hit with a module-level dispatch dict,
    # and it was found the same way: by the test passing for the wrong
    # reason (capture succeeded, so the safety property went unproven).
    monkeypatch.setattr("app.services.pilot_metrics.compute_note_burden", _boom)
    from app.services.note_lifecycle import transition

    signed = transition(db, note, NoteStatus.SIGNED, clinician_id=c.id, prc_license_number="PRC-1")

    assert signed.status is NoteStatus.SIGNED
    assert signed.signed_at is not None
    assert db.query(NoteQualityMetric).filter(NoteQualityMetric.note_id == note.id).count() == 0


def test_capture_is_idempotent_per_definition_version(db):
    c = _doctor(db)
    note = _note(db, _encounter(db, c), status=NoteStatus.SIGNED)
    note.signed_at = datetime.now(timezone.utc)
    note.signed_by_clinician_id = c.id
    db.add(note)
    db.commit()

    capture_note_quality(db, note)
    capture_note_quality(db, note)

    assert db.query(NoteQualityMetric).filter(NoteQualityMetric.note_id == note.id).count() == 1


def test_documentation_time_separates_total_from_review(db):
    """A doctor who leaves a note open over lunch inflates total and not
    review. Conflating them makes the headline figure unusable.
    """
    c = _doctor(db)
    started = datetime.now(timezone.utc) - timedelta(minutes=40)
    e = _encounter(db, c, created_at=started)
    note = _note(db, e, status=NoteStatus.SIGNED)
    note.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    note.signed_at = datetime.now(timezone.utc)
    note.signed_by_clinician_id = c.id
    db.add(note)
    db.commit()

    row = capture_note_quality(db, note)

    assert row is not None
    assert row.total_seconds is not None and row.total_seconds > row.review_seconds
    assert 250 <= row.review_seconds <= 350


# --- the report -----------------------------------------------------------


def test_no_data_reports_none_not_zero(db):
    """0.0 reads as "failing badly"; None reads as "no data". A dashboard
    must not render them identically.
    """
    summary = edit_burden_summary(db)

    assert summary.minor_only_rate is None
    assert summary.measured_notes == 0
    assert rating_summary(db).mean_stars is None
    assert documentation_time_summary(db).median_total_seconds is None


def test_edit_burden_rate_and_coverage(db):
    c = _doctor(db)
    for i in range(4):
        e = _encounter(db, c, key=f"idem-rate-{i}")
        note = _note(db, e, status=NoteStatus.SIGNED)
        note.signed_at = datetime.now(timezone.utc)
        db.add(note)
        db.commit()
        if i == 3:
            _edit(db, note, c, "assessment", _DRAFT, "A completely different assessment entirely.")
        capture_note_quality(db, note)

    summary = edit_burden_summary(db)

    assert summary.measured_notes == 4
    assert summary.minor_only == 3
    assert summary.minor_only_rate == 0.75
    assert summary.coverage == 1.0


def test_coverage_exposes_notes_that_were_never_measured(db):
    """Below 1.0 means capture lost notes, and the headline rate is computed
    over a biased sample. Silent loss is the thing that would make this
    metric quietly untrue.
    """
    c = _doctor(db)
    for i in range(3):
        note = _note(db, _encounter(db, c, key=f"idem-cov-{i}"), status=NoteStatus.SIGNED)
        note.signed_at = datetime.now(timezone.utc)
        db.add(note)
        db.commit()
        if i == 0:
            capture_note_quality(db, note)

    summary = edit_burden_summary(db)

    assert summary.signed_notes == 3
    assert summary.measured_notes == 1
    assert summary.coverage is not None and summary.coverage < 1.0


def test_usage_counts_distinct_weeks_not_just_volume(db):
    """ "Still using it in week 4" is about spread. One 40-encounter day
    followed by silence is exactly the pattern a total would hide.
    """
    c = _doctor(db)
    now = datetime.now(timezone.utc)
    for i, offset in enumerate([0, 1, 8, 15]):
        _encounter(db, c, key=f"idem-use-{i}", created_at=now - timedelta(days=offset))

    usage = clinician_usage(db, since_days=28)

    assert len(usage) == 1
    assert usage[0].encounters == 4
    assert usage[0].weeks_active >= 3


def test_filing_summary_counts_unlinked_notes(db):
    c = _doctor(db)
    linked = _note(db, _encounter(db, c, key="idem-f-1"), status=NoteStatus.SIGNED)
    unlinked = _note(db, _encounter(db, c, key="idem-f-2", patient=False), status=NoteStatus.SIGNED)
    db.add_all([linked, unlinked])
    db.commit()

    summary = filing_summary(db)

    assert summary.signed_notes == 2
    assert summary.linked_to_patient == 1
    assert summary.unlinked == 1


def test_review_sample_is_deterministic_and_flags_first(db):
    """Two reviewers must get the same notes, and the ones worth reading
    must come first.
    """
    c = _doctor(db)
    signed_at = datetime.now(timezone.utc) - timedelta(days=1)
    for i in range(5):
        note = _note(db, _encounter(db, c, key=f"idem-rev-{i}"), status=NoteStatus.SIGNED)
        note.signed_at = signed_at
        db.add(note)
        db.commit()
        if i in (1, 3):
            _edit(db, note, c, "assessment", _DRAFT, _DRAFT.replace("500mg", "5000mg"))
        capture_note_quality(db, note)

    first = review_sample(db, sample_size=3)
    second = review_sample(db, sample_size=3)

    assert first == second
    flagged = {r.note_id for r in db.query(NoteQualityMetric).all() if r.safety_flagged_sections}
    assert len(flagged) == 2
    assert set(first[:2]) == flagged


# --- routes ---------------------------------------------------------------


def test_a_doctor_can_rate_an_encounter(db, client):
    c = _doctor(db)
    e = _encounter(db, c)

    response = client.post(
        f"/api/v1/pilot/encounters/{e.id}/rating",
        json={"stars": 4, "comment": "Saved me time."},
        headers=_auth(c),
    )

    assert response.status_code == 201
    assert response.json()["stars"] == 4


def test_rating_twice_updates_rather_than_duplicates(db, client):
    c = _doctor(db)
    e = _encounter(db, c)

    client.post(f"/api/v1/pilot/encounters/{e.id}/rating", json={"stars": 2}, headers=_auth(c))
    client.post(f"/api/v1/pilot/encounters/{e.id}/rating", json={"stars": 5}, headers=_auth(c))

    rows = db.query(EncounterRating).filter(EncounterRating.encounter_id == e.id).all()
    assert len(rows) == 1
    assert rows[0].stars == 5


@pytest.mark.parametrize("stars", [0, 6, -1])
def test_out_of_range_stars_are_rejected(db, client, stars):
    c = _doctor(db)
    e = _encounter(db, c)

    response = client.post(f"/api/v1/pilot/encounters/{e.id}/rating", json={"stars": stars}, headers=_auth(c))

    assert response.status_code == 422


def test_rating_an_unknown_encounter_is_404(db, client):
    c = _doctor(db)

    response = client.post("/api/v1/pilot/encounters/does-not-exist/rating", json={"stars": 3}, headers=_auth(c))

    assert response.status_code == 404


def test_the_rating_comment_is_encrypted_at_rest(db, client):
    """The one field here a doctor could type a patient's name into."""
    from sqlalchemy import text

    c = _doctor(db)
    e = _encounter(db, c)
    client.post(
        f"/api/v1/pilot/encounters/{e.id}/rating",
        json={"stars": 5, "comment": "Maria Santos was happy"},
        headers=_auth(c),
    )

    raw = db.execute(text("SELECT comment FROM encounter_ratings")).scalar()
    assert raw is not None
    assert "Maria" not in raw


def test_the_pilot_report_returns_no_phi(db, client):
    c = _doctor(db)
    note = _note(db, _encounter(db, c), status=NoteStatus.SIGNED)
    note.signed_at = datetime.now(timezone.utc)
    db.add(note)
    db.commit()
    capture_note_quality(db, note)

    response = client.get("/api/v1/pilot/report", headers=_auth(c))

    assert response.status_code == 200
    body = response.text
    # Nothing from the note or the patient may appear anywhere in it.
    assert "paracetamol" not in body
    assert "Maria" not in body
    assert response.json()["edit_burden"]["definition_version"] == DEFINITION_VERSION


def test_compliance_can_read_the_report_without_clinical_access(db, client):
    """The person answering "did the pilot pass?" should not have to ask a
    doctor to read them the numbers.
    """
    _doctor(db)
    reviewer = Clinician(
        email="compliance@example.com",
        full_name="C. Officer",
        hashed_password="x",
        role="compliance",
    )
    db.add(reviewer)
    db.commit()
    db.refresh(reviewer)

    assert client.get("/api/v1/pilot/report", headers=_auth(reviewer)).status_code == 200


def test_the_review_sample_returns_ids_only(db, client):
    """An endpoint that returned note text would be an unaudited bulk PHI
    export wearing a different hat.
    """
    c = _doctor(db)
    note = _note(db, _encounter(db, c), status=NoteStatus.SIGNED)
    note.signed_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.add(note)
    db.commit()
    capture_note_quality(db, note)

    response = client.get("/api/v1/pilot/review-sample", headers=_auth(c))

    assert response.status_code == 200
    assert response.json()["note_ids"] == [note.id]
    assert "paracetamol" not in response.text


def test_pilot_reads_are_audited(db, client):
    from app.models.audit_log import AuditLog

    c = _doctor(db)
    client.get("/api/v1/pilot/report", headers=_auth(c))
    client.get("/api/v1/pilot/review-sample", headers=_auth(c))

    actions = {row.action for row in db.query(AuditLog).all()}
    assert "pilot.report.read" in actions
    assert "pilot.review_sample.read" in actions
