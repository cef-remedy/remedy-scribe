"""Phase 4.4: retention enforcement.

Deletion is the one operation in this system that cannot be undone, so
these tests are weighted toward proving what is *not* deleted. Every
purge case is paired with a "and nothing else went with it" assertion,
and the signed-note boundary — a permanent medical record, never
collectable — is asserted on its own rather than left implied.

The other half is the grounding interaction. `app/services/grounding.py`
reads three signals this job writes or removes: `audio_deleted_at` (with
the consent ledger deciding whether that reads as WITHDRAWN or EXPIRED),
the presence of a `Transcript` row, and the presence of `NoteRevision`
rows. A deletion job that leaves any of those saying the wrong thing
makes the grounding UI confidently wrong, which decision 0030 argues is
worse than showing nothing at all.
"""

from datetime import datetime, timedelta, timezone

from app.models.audit_log import AuditLog
from app.models.clinician import Clinician
from app.models.consent import ConsentEventType, ConsentLedgerEntry
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note, NoteRevision, NoteStatus
from app.models.transcript import Transcript
from app.services.grounding import AudioState, TranscriptState, resolve_grounding
from app.tasks.retention import (
    PurgeReason,
    purge_encounter,
    purge_withdrawn_encounter,
    run_retention_sweep,
    sweep_expired_retention,
)

_AUDIO_KEY = "encounters/e/audio/deadbeef.opus"
_ASSESSMENT = "Likely community-acquired pneumonia. Consider chest radiograph."
#: Matches the offsets generation would have written for the two sentences
#: above — spelled out literally, the same convention tests/test_grounding.py
#: uses, so grounding resolves real spans here rather than an empty note.
_SPANS = (
    '{"assessment": {"suppressed": false, "spans": ['
    '{"text_start": 0, "text_end": 36, "segment_ids": ["seg0"]},'
    '{"text_start": 37, "text_end": 63, "segment_ids": ["seg1"]}]}}'
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(days: int) -> datetime:
    return _now() - timedelta(days=days)


def _ahead(days: int) -> datetime:
    return _now() + timedelta(days=days)


def _segments(count: int = 2) -> list[dict]:
    return [
        {
            "id": f"seg{i}",
            "speaker": "speaker_0",
            "words": [
                {
                    "text": f"word{i}",
                    "start_ms": i * 1_000,
                    "end_ms": i * 1_000 + 400,
                    "confidence": 0.95,
                    "speaker": "speaker_0",
                }
            ],
        }
        for i in range(count)
    ]


def _doctor(db, email: str = "doc@example.com") -> Clinician:
    clinician = Clinician(email=email, full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician


def _encounter(
    db,
    doctor: Clinician,
    *,
    key: str = "idem-4.4",
    audio_object_key: str | None = _AUDIO_KEY,
    audio_expires_at: datetime | None = None,
    audio_deleted_at: datetime | None = None,
) -> Encounter:
    encounter = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key=key,
        audio_object_key=audio_object_key,
        audio_retention_expires_at=audio_expires_at,
        audio_deleted_at=audio_deleted_at,
        pipeline_status=EncounterPipelineStatus.NOTE_GENERATED,
    )
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    return encounter


def _transcript(db, encounter_id: str, *, expires_at: datetime | None) -> Transcript:
    row = Transcript(
        encounter_id=encounter_id,
        asr_provider="fake-asr",
        asr_model_version="v1",
        segments=_segments(),
        retention_expires_at=expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _note(db, encounter_id: str, *, status: NoteStatus = NoteStatus.GENERATED) -> Note:
    note = Note(
        encounter_id=encounter_id,
        status=status,
        assessment=_ASSESSMENT,
        plan="Amoxicillin 500mg three times daily.",
        subjective="Cough for three days.",
        objective="Temp 38.1C.",
        source_spans=_SPANS,
        note_generator_provider="haiku",
        prompt_version="v1",
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def _revision(db, note: Note, doctor: Clinician, *, section: str = "assessment") -> NoteRevision:
    revision = NoteRevision(
        note_id=note.id,
        section=section,
        previous_text="before",
        new_text="after",
        edited_by_clinician_id=doctor.id,
    )
    db.add(revision)
    db.commit()
    db.refresh(revision)
    return revision


def _withdraw(db, encounter_id: str) -> None:
    db.add(
        ConsentLedgerEntry(
            encounter_id=encounter_id,
            event=ConsentEventType.WITHDRAWN,
            participant_roster="[]",
            purposes="[]",
            script_language="en",
        )
    )
    db.commit()


def _consent_given(db, encounter_id: str) -> None:
    db.add(
        ConsentLedgerEntry(
            encounter_id=encounter_id,
            event=ConsentEventType.GIVEN,
            participant_roster="[]",
            purposes="[]",
            script_language="en",
        )
    )
    db.commit()


def _deletes_ok(monkeypatch) -> list[str]:
    """Records the keys handed to object storage. Patching the module
    attribute works only because retention.py calls
    `storage.delete_object(...)` rather than importing the function by
    name — the Phase 1.5 lesson, restated as a test dependency.
    """
    calls: list[str] = []

    def _delete(key: str) -> bool:
        calls.append(key)
        return True

    monkeypatch.setattr("app.services.storage.delete_object", _delete)
    return calls


def _actions(db) -> list[str]:
    return [row.action for row in db.query(AuditLog).order_by(AuditLog.created_at.asc()).all()]


# --- the ordinary path: an expired encounter -----------------------------


def test_sweep_deletes_audio_whose_retention_clock_has_run_out(db, monkeypatch):
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ago(1))

    counts = run_retention_sweep(db)

    assert counts["audio"] == 1
    assert deleted == [_AUDIO_KEY]
    db.refresh(encounter)
    assert encounter.audio_deleted_at is not None
    # The key stays on the row: it is what tells grounding the difference
    # between "deleted" and NEVER_RECORDED.
    assert encounter.audio_object_key == _AUDIO_KEY


def test_sweep_leaves_audio_whose_clock_has_not_run_out(db, monkeypatch):
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ahead(89))
    _transcript(db, encounter.id, expires_at=_ahead(89))

    counts = run_retention_sweep(db)

    assert counts == {"encounters": 0, "audio": 0, "transcripts": 0, "revisions": 0}
    assert deleted == []
    db.refresh(encounter)
    assert encounter.audio_deleted_at is None
    assert db.query(Transcript).count() == 1


def test_a_null_retention_clock_is_never_expired(db, monkeypatch):
    """NULL means "no policy clock was ever set for this row", not "expired
    long ago". Failing open here would delete rows the policy never covered.
    """
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=None)
    _transcript(db, encounter.id, expires_at=None)

    counts = run_retention_sweep(db)

    assert counts["encounters"] == 0
    assert deleted == []
    assert db.query(Transcript).count() == 1


def test_sweep_deletes_an_expired_transcript_and_its_note_revisions(db, monkeypatch):
    _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ago(1))
    _transcript(db, encounter.id, expires_at=_ago(1))
    note = _note(db, encounter.id)
    _revision(db, note, doctor)
    _revision(db, note, doctor, section="plan")

    counts = run_retention_sweep(db)

    assert counts == {"encounters": 1, "audio": 1, "transcripts": 1, "revisions": 2}
    assert db.query(Transcript).count() == 0
    assert db.query(NoteRevision).count() == 0
    # The record itself is untouched, down to its text.
    surviving = db.get(Note, note.id)
    assert surviving is not None
    assert surviving.assessment == _ASSESSMENT


# --- the boundary that must never move -----------------------------------


def test_a_signed_note_is_never_deleted_however_expired(db, monkeypatch):
    """A signed note is a permanent medical record. Its audio, transcript
    and drafting history all expire; the note does not, and neither does
    the signature on it.
    """
    _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ago(400))
    _transcript(db, encounter.id, expires_at=_ago(400))
    note = _note(db, encounter.id, status=NoteStatus.SIGNED)
    note.signed_by_clinician_id = doctor.id
    note.signed_prc_license_number = "PRC-12345"
    note.signed_at = _ago(399)
    db.add(note)
    db.commit()
    _revision(db, note, doctor)

    run_retention_sweep(db)

    signed = db.get(Note, note.id)
    assert signed is not None
    assert signed.status == NoteStatus.SIGNED
    assert signed.signed_prc_license_number == "PRC-12345"
    assert signed.assessment == _ASSESSMENT
    assert db.query(Note).count() == 1
    # ...while everything derived from the recording did go.
    assert db.query(Transcript).count() == 0
    assert db.query(NoteRevision).count() == 0


def test_the_consent_ledger_survives_the_purge_that_it_triggered(db, monkeypatch):
    """The ledger is the legal record of the withdrawal. Deleting it while
    acting on it would destroy the only evidence that the deletion was
    lawful — and it is what grounding reads to say *why* the audio is gone.
    """
    _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ahead(89))
    _consent_given(db, encounter.id)
    _withdraw(db, encounter.id)
    _transcript(db, encounter.id, expires_at=_ahead(89))

    run_retention_sweep(db)

    assert db.query(ConsentLedgerEntry).count() == 2


def test_an_encounter_belonging_to_someone_else_is_not_collateral(db, monkeypatch):
    """The narrowest possible statement of "deletes exactly what is
    expired": two encounters, one expired, one not, everything else equal.
    """
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    expired = _encounter(db, doctor, key="idem-expired", audio_expires_at=_ago(1))
    _transcript(db, expired.id, expires_at=_ago(1))
    fresh = _encounter(
        db,
        doctor,
        key="idem-fresh",
        audio_object_key="encounters/f/audio/cafe.opus",
        audio_expires_at=_ahead(1),
    )
    fresh_transcript = _transcript(db, fresh.id, expires_at=_ahead(1))
    fresh_note = _note(db, fresh.id)
    _revision(db, fresh_note, doctor)

    counts = run_retention_sweep(db)

    assert counts["encounters"] == 1
    assert deleted == [_AUDIO_KEY]
    db.refresh(fresh)
    assert fresh.audio_deleted_at is None
    assert db.get(Transcript, fresh_transcript.id) is not None
    assert db.query(NoteRevision).count() == 1


# --- idempotence and failure ---------------------------------------------


def test_a_second_sweep_deletes_nothing_and_writes_no_second_audit_row(db, monkeypatch):
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ago(1))
    _transcript(db, encounter.id, expires_at=_ago(1))
    note = _note(db, encounter.id)
    _revision(db, note, doctor)

    first = run_retention_sweep(db)
    audit_after_first = _actions(db)
    second = run_retention_sweep(db)

    assert first["encounters"] == 1
    assert second == {"encounters": 0, "audio": 0, "transcripts": 0, "revisions": 0}
    assert deleted == [_AUDIO_KEY]  # not deleted twice
    assert _actions(db) == audit_after_first


def test_a_storage_failure_does_not_stamp_a_deletion_that_did_not_happen(db, monkeypatch):
    """`audio_deleted_at` is a claim that the bytes are gone. Stamping it on
    a failed delete would make grounding report EXPIRED for a recording that
    is still sitting in the bucket — and no later sweep would retry it.
    """
    monkeypatch.setattr("app.services.storage.delete_object", lambda key: False)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ago(1))

    counts = run_retention_sweep(db)

    assert counts["audio"] == 0
    db.refresh(encounter)
    assert encounter.audio_deleted_at is None
    assert _actions(db) == []

    # ...and the next sweep picks it up again once storage is back.
    calls = _deletes_ok(monkeypatch)
    assert run_retention_sweep(db)["audio"] == 1
    assert calls == [_AUDIO_KEY]


def test_the_sweep_is_bounded_and_resumes_where_it_left_off(db, monkeypatch):
    _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    for i in range(3):
        _encounter(
            db,
            doctor,
            key=f"idem-batch-{i}",
            audio_object_key=f"encounters/b{i}/audio/x.opus",
            audio_expires_at=_ago(1),
        )

    assert run_retention_sweep(db, limit=1)["encounters"] == 1
    assert run_retention_sweep(db, limit=1)["encounters"] == 1
    assert run_retention_sweep(db, limit=1)["encounters"] == 1
    assert run_retention_sweep(db, limit=1)["encounters"] == 0
    assert db.query(Encounter).filter(Encounter.audio_deleted_at.is_(None)).count() == 0


# --- the audit trail (P0-8) ----------------------------------------------


def test_every_deletion_is_audited_without_naming_what_was_deleted(db, monkeypatch):
    _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ago(1))
    transcript = _transcript(db, encounter.id, expires_at=_ago(1))
    transcript_id = transcript.id
    note = _note(db, encounter.id)
    _revision(db, note, doctor)

    run_retention_sweep(db)

    rows = db.query(AuditLog).all()
    by_action = {row.action: row for row in rows}
    assert set(by_action) == {
        "encounter.audio.delete",
        "encounter.transcript.delete",
        "note.revisions.delete",
    }
    assert by_action["encounter.audio.delete"].entity_id == encounter.id
    assert by_action["encounter.transcript.delete"].entity_id == transcript_id
    assert by_action["note.revisions.delete"].entity_id == note.id
    # Nobody triggered this; a NULL actor is the honest answer.
    assert all(row.actor_clinician_id is None for row in rows)
    for row in rows:
        assert '"reason": "retention_expired"' in (row.diff or "")
        # The object key is a direct pointer at PHI bytes and an audit row
        # outlives what it points at (decision 0030's reasoning, restated
        # for deletions). Nor is any note/transcript text recorded.
        assert _AUDIO_KEY not in (row.diff or "")
        assert _ASSESSMENT not in (row.diff or "")


def test_a_withdrawal_purge_is_audited_as_a_withdrawal_not_an_expiry(db, monkeypatch):
    _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ahead(89))
    _consent_given(db, encounter.id)
    _withdraw(db, encounter.id)
    _transcript(db, encounter.id, expires_at=_ahead(89))

    purge_withdrawn_encounter(db, encounter.id, actor_clinician_id=doctor.id)

    rows = db.query(AuditLog).all()
    assert {row.action for row in rows} == {
        "encounter.audio.delete",
        "encounter.transcript.delete",
    }
    for row in rows:
        assert '"reason": "consent_withdrawn"' in (row.diff or "")
        # A withdrawal has a human behind it, unlike the scheduled sweep.
        assert row.actor_clinician_id == doctor.id


# --- withdrawal: the immediate path (P0-1) -------------------------------


def test_withdrawal_deletes_derived_phi_immediately_not_in_ninety_days(db, monkeypatch):
    """`handle_withdrawal` alone deletes the audio and sets the retention
    clock. It does not reach the transcript — which is verbatim PHI,
    arguably more sensitive than the recording — or the drafting history.
    """
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ahead(89))
    _consent_given(db, encounter.id)
    _withdraw(db, encounter.id)
    _transcript(db, encounter.id, expires_at=_ahead(89))
    note = _note(db, encounter.id)
    _revision(db, note, doctor)

    outcome = purge_withdrawn_encounter(db, encounter.id)

    assert outcome.audio_deleted is True
    assert outcome.pipeline_will_stop is True
    assert deleted == [_AUDIO_KEY]
    assert db.query(Transcript).count() == 0
    assert db.query(NoteRevision).count() == 0
    assert db.get(Note, note.id) is not None  # still the record


def test_withdrawal_is_idempotent(db, monkeypatch):
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ahead(89))
    _consent_given(db, encounter.id)
    _withdraw(db, encounter.id)
    _transcript(db, encounter.id, expires_at=_ahead(89))

    first = purge_withdrawn_encounter(db, encounter.id)
    second = purge_withdrawn_encounter(db, encounter.id)

    assert first.audio_deleted is True
    assert second.audio_deleted is True  # still gone, reported honestly
    assert deleted == [_AUDIO_KEY]
    assert _actions(db).count("encounter.audio.delete") == 1


def test_the_sweep_backstops_a_withdrawal_whose_derived_rows_survived(db, monkeypatch):
    """`handle_withdrawal` is what the consent route calls today, and it
    leaves the transcript behind. Until that call site adopts
    `purge_withdrawn_encounter`, the sweep is what collects the remainder —
    within the hour, not in 90 days, since it keys off the ledger rather
    than the transcript's own clock.
    """
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ahead(89))
    _consent_given(db, encounter.id)
    _withdraw(db, encounter.id)
    _transcript(db, encounter.id, expires_at=_ahead(89))
    note = _note(db, encounter.id)
    _revision(db, note, doctor)

    counts = run_retention_sweep(db)

    assert counts == {"encounters": 1, "audio": 1, "transcripts": 1, "revisions": 1}
    assert deleted == [_AUDIO_KEY]
    assert db.query(Transcript).count() == 0
    assert _actions(db).count("encounter.audio.delete") == 1


def test_re_consent_after_withdrawal_stops_the_sweep_deleting_anything(db, monkeypatch):
    """The ledger is a fold, not a latch (`current_consent_state`): a later
    "given" restores consent. Treating any historical withdrawal as
    permission to delete would destroy PHI the patient has since agreed to.
    """
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ahead(89))
    _consent_given(db, encounter.id)
    _withdraw(db, encounter.id)
    _consent_given(db, encounter.id)
    _transcript(db, encounter.id, expires_at=_ahead(89))

    counts = run_retention_sweep(db)

    assert counts["encounters"] == 0
    assert deleted == []
    assert db.query(Transcript).count() == 1


# --- interaction with grounding (Phase 3, decision 0030) -----------------


def test_retention_expiry_reads_as_expiry_and_withdrawal_as_withdrawal(db, monkeypatch):
    """Both encounters end up in the identical observable state — no audio
    object, no transcript row — and grounding must still tell a doctor
    *why*. It gets that from the consent ledger, which this job never
    touches.
    """
    _deletes_ok(monkeypatch)
    doctor = _doctor(db)

    expired = _encounter(db, doctor, key="idem-g-expired", audio_expires_at=_ago(1))
    _consent_given(db, expired.id)
    _transcript(db, expired.id, expires_at=_ago(1))
    expired_note = _note(db, expired.id)

    withdrawn = _encounter(
        db,
        doctor,
        key="idem-g-withdrawn",
        audio_object_key="encounters/w/audio/x.opus",
        audio_expires_at=_ahead(89),
    )
    _consent_given(db, withdrawn.id)
    _withdraw(db, withdrawn.id)
    _transcript(db, withdrawn.id, expires_at=_ahead(89))
    withdrawn_note = _note(db, withdrawn.id)

    run_retention_sweep(db)

    expired_grounding = resolve_grounding(db, db.get(Note, expired_note.id))
    withdrawn_grounding = resolve_grounding(db, db.get(Note, withdrawn_note.id))

    assert expired_grounding.audio_state is AudioState.EXPIRED
    assert withdrawn_grounding.audio_state is AudioState.WITHDRAWN
    # A note exists, so a transcript existed once — its absence is a
    # deletion, never "never transcribed".
    assert expired_grounding.transcript_state is TranscriptState.EXPIRED
    # This assertion originally read EXPIRED, pinning the only answer the
    # transcript ladder could give at the time — and this suite's own
    # follow-up note called that out as a gap: the purge deletes a
    # withdrawn encounter's transcript too, so describing it as "the
    # retention period elapsed" told the doctor the wrong reason.
    # `TranscriptState` gained a WITHDRAWN rung for exactly the reason
    # decision 0030 gave the audio ladder five: the two are observably
    # identical and mean entirely different things. The job below is still
    # reason-neutral — it reads the ledger, it does not decide.
    assert withdrawn_grounding.transcript_state is TranscriptState.WITHDRAWN
    assert expired_grounding.segments == []


def test_grounding_never_reports_an_edited_section_as_the_models_words(db, monkeypatch):
    """The trap this job could have walked into.

    `edited_since_generation` is derived from a `NoteRevision` merely
    existing. Deleting revisions while the transcript survived would flip a
    doctor-rewritten section back to "the model wrote this" *and* leave
    real transcript passages for the UI to highlight as its source —
    grounding confidently wrong, which decision 0030 calls worse than
    showing nothing. So the two are only ever removed together.
    """
    _deletes_ok(monkeypatch)
    # The audio still exists at this point, and `_audio_state` verifies that
    # against storage rather than trusting the row (decision 0030) — stubbed
    # so this test asserts about grounding, not about a reachable bucket.
    monkeypatch.setattr("app.services.storage.head_object", lambda key: {"ContentLength": 1})
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ahead(89))
    _consent_given(db, encounter.id)
    transcript = _transcript(db, encounter.id, expires_at=_ahead(89))
    note = _note(db, encounter.id)
    _revision(db, note, doctor, section="assessment")

    # Nothing is due yet: the revision must survive as long as the
    # transcript it would otherwise mis-attribute.
    run_retention_sweep(db)
    before = resolve_grounding(db, db.get(Note, note.id))
    assert db.get(Transcript, transcript.id) is not None
    assert before.sections["assessment"].edited_since_generation is True
    assert before.segments  # real passages the UI would highlight

    # Now expire both clocks. The revision goes, but so does the transcript,
    # so there is nothing left to mis-attribute the doctor's prose to.
    encounter.audio_retention_expires_at = _ago(1)
    transcript.retention_expires_at = _ago(1)
    db.add_all([encounter, transcript])
    db.commit()

    run_retention_sweep(db)

    after = resolve_grounding(db, db.get(Note, note.id))
    assert db.query(NoteRevision).count() == 0
    assert after.transcript_state is TranscriptState.EXPIRED
    assert after.segments == []
    assert after.sections["assessment"].edited_since_generation is False


def test_revisions_are_never_deleted_while_their_transcript_survives(db, monkeypatch):
    """The same invariant stated directly, for the case where only the
    audio clock has run out — audio expiring is not licence to drop the
    drafting history.
    """
    _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ago(1))
    _transcript(db, encounter.id, expires_at=_ahead(30))
    note = _note(db, encounter.id)
    _revision(db, note, doctor)

    counts = run_retention_sweep(db)

    assert counts["audio"] == 1
    assert counts["transcripts"] == 0
    assert counts["revisions"] == 0
    assert db.query(NoteRevision).count() == 1


# --- the Celery task wrapper ---------------------------------------------


def test_the_beat_task_runs_the_sweep_with_its_own_session(db, monkeypatch):
    """Exercises the task as Beat will invoke it — no arguments, its own
    `SessionLocal`. Calling `sweep_expired_retention()` reaches the real
    `storage.delete_object` unless it is patched by module attribute, which
    is exactly the Phase 1.5 failure mode restated for this job.
    """
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ago(1))
    _transcript(db, encounter.id, expires_at=_ago(1))

    counts = sweep_expired_retention()

    assert counts == {"encounters": 1, "audio": 1, "transcripts": 1, "revisions": 0}
    assert deleted == [_AUDIO_KEY]
    db.expire_all()
    assert db.get(Encounter, encounter.id).audio_deleted_at is not None


def test_purge_encounter_refuses_to_act_early_without_force(db, monkeypatch):
    """`force` is the withdrawal path's licence to ignore the clocks, and
    the only one. Called without it, a purge on an unexpired encounter is a
    no-op rather than an early deletion.
    """
    deleted = _deletes_ok(monkeypatch)
    doctor = _doctor(db)
    encounter = _encounter(db, doctor, audio_expires_at=_ahead(89))
    _transcript(db, encounter.id, expires_at=_ahead(89))

    result = purge_encounter(db, encounter, reason=PurgeReason.RETENTION_EXPIRED)

    assert result.anything_deleted is False
    assert deleted == []
    assert db.query(Transcript).count() == 1
