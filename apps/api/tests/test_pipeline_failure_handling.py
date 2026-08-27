"""Phase 1.5: pipeline failure handling.

Task-level tests call `transcribe_encounter`/`generate_note` via Celery's
own `.apply(args=[...])` rather than as a plain function call. That
distinction matters here specifically: calling a `bind=True` task
directly (as earlier phases' happy-path tests do) runs it exactly once
and re-raises whatever `self.retry()` raises with no real retry loop —
`self.request.retries` never leaves 0. `.apply()` is Celery's real eager
executor: it actually re-invokes the task on each `Retry`, incrementing
`self.request.retries` each time, until `max_retries` is hit — the only
way to exercise "retries exhausted" without a live worker/broker.
"""

from datetime import datetime, timedelta, timezone

from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note
from app.services.asr.base import TranscriptSegment, TranscriptWord
from app.services.note_generation.base import GeneratedNote, GeneratedSection
from app.services.transcripts import persist_transcript
from app.tasks.pipeline import generate_note, sweep_stuck_encounters, transcribe_encounter

_SAMPLE_SEGMENTS = [
    TranscriptSegment(
        speaker="speaker_0",
        words=[TranscriptWord(text="Ano", start_ms=0, end_ms=200, confidence=0.95, speaker="speaker_0")],
    )
]


def _seed_encounter(db, *, role: str = "doctor") -> tuple[Encounter, Clinician]:
    clinician = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role=role)
    db.add(clinician)
    db.commit()
    db.refresh(clinician)

    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key="idem-1.5")
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    db.add(
        ConsentLedgerEntry(
            encounter_id=encounter.id,
            event="given",
            participant_roster="[]",
            purposes="[]",
            script_language="en",
        )
    )
    db.commit()
    return encounter, clinician


def _token(clinician: Clinician) -> str:
    from app.core.security import create_access_token

    return create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})


def _auth(clinician: Clinician) -> dict:
    return {"Authorization": f"Bearer {_token(clinician)}"}


# --- transcribe_encounter: dead-letter after exhausting retries -----------


def test_transcribe_encounter_dead_letters_after_max_retries(db, monkeypatch):
    class _AlwaysFailsProvider:
        provider_name = "fake-asr"
        model_version = "fake-v1"

        def transcribe(self, audio_object_key: str) -> list[TranscriptSegment]:
            raise RuntimeError("ASR vendor unreachable")

    monkeypatch.setattr("app.tasks.pipeline.get_asr_provider", lambda: _AlwaysFailsProvider())

    encounter, _clinician = _seed_encounter(db)
    encounter.audio_object_key = "encounters/x/audio/y.m4a"
    encounter.pipeline_status = EncounterPipelineStatus.UPLOADED
    db.add(encounter)
    db.commit()

    result = transcribe_encounter.apply(args=[encounter.id])
    assert result.state == "FAILURE"

    db.refresh(encounter)
    assert encounter.pipeline_status == EncounterPipelineStatus.TRANSCRIPTION_FAILED
    assert encounter.retry_count == transcribe_encounter.max_retries + 1
    assert "ASR vendor unreachable" in encounter.last_pipeline_error


def test_transcribe_encounter_recovers_from_a_transient_failure(db, monkeypatch):
    """Fails twice, then succeeds on the third attempt — well within
    max_retries=3 — and must NOT be dead-lettered: retry_count resets to
    0 and last_pipeline_error clears once the stage actually succeeds,
    so neither stays around as stale history from a failed attempt.
    """
    calls = {"n": 0}

    class _FlakyProvider:
        provider_name = "fake-asr"
        model_version = "fake-v1"

        def transcribe(self, audio_object_key: str) -> list[TranscriptSegment]:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient network error")
            return _SAMPLE_SEGMENTS

    monkeypatch.setattr("app.tasks.pipeline.get_asr_provider", lambda: _FlakyProvider())

    encounter, _clinician = _seed_encounter(db)
    encounter.audio_object_key = "encounters/x/audio/y.m4a"
    encounter.pipeline_status = EncounterPipelineStatus.UPLOADED
    db.add(encounter)
    db.commit()

    result = transcribe_encounter.apply(args=[encounter.id])
    assert result.state == "SUCCESS"

    db.refresh(encounter)
    assert encounter.pipeline_status == EncounterPipelineStatus.TRANSCRIBED
    assert encounter.retry_count == 0
    assert encounter.last_pipeline_error is None
    assert calls["n"] == 3


# --- generate_note: same dead-letter contract, its own failure mode ------


class _AlwaysFailsGenerator:
    def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
        raise RuntimeError("Anthropic API unreachable")


def test_generate_note_dead_letters_after_max_retries(db, monkeypatch):
    monkeypatch.setattr("app.tasks.pipeline.get_note_generator", lambda: _AlwaysFailsGenerator())

    encounter, _clinician = _seed_encounter(db)
    persist_transcript(db, encounter.id, provider_name="groq_whisper_large_v3", segments=_SAMPLE_SEGMENTS)
    encounter.pipeline_status = EncounterPipelineStatus.TRANSCRIBED
    db.add(encounter)
    db.commit()

    result = generate_note.apply(args=[encounter.id])
    assert result.state == "FAILURE"

    db.refresh(encounter)
    assert encounter.pipeline_status == EncounterPipelineStatus.GENERATION_FAILED
    assert encounter.retry_count == generate_note.max_retries + 1
    assert "Anthropic API unreachable" in encounter.last_pipeline_error
    # The dead letter records the failure — it must not also leave a
    # half-written Note row behind for a /retry to trip over.
    assert db.query(Note).filter(Note.encounter_id == encounter.id).one_or_none() is None


def test_generate_note_recovers_from_a_transient_failure(db, monkeypatch):
    calls = {"n": 0}
    empty = GeneratedSection(text="ok")

    class _FlakyGenerator:
        def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("rate limited")
            return GeneratedNote(
                assessment=empty, plan=empty, subjective=empty, objective=empty, provider="haiku", prompt_version="v1"
            )

    monkeypatch.setattr("app.tasks.pipeline.get_note_generator", lambda: _FlakyGenerator())

    encounter, _clinician = _seed_encounter(db)
    persist_transcript(db, encounter.id, provider_name="groq_whisper_large_v3", segments=_SAMPLE_SEGMENTS)
    encounter.pipeline_status = EncounterPipelineStatus.TRANSCRIBED
    db.add(encounter)
    db.commit()

    result = generate_note.apply(args=[encounter.id])
    assert result.state == "SUCCESS"

    db.refresh(encounter)
    assert encounter.pipeline_status == EncounterPipelineStatus.NOTE_GENERATED
    assert encounter.retry_count == 0
    assert encounter.last_pipeline_error is None


# --- GET /encounters/failed: dead-letter surfacing -------------------------


def test_list_failed_encounters_returns_only_dead_lettered_ones(db, client):
    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    ok = Encounter(
        clinician_id=doctor.id, upload_idempotency_key="idem-ok", pipeline_status=EncounterPipelineStatus.NOTE_GENERATED
    )
    failed_a = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key="idem-failed-a",
        pipeline_status=EncounterPipelineStatus.TRANSCRIPTION_FAILED,
    )
    failed_b = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key="idem-failed-b",
        pipeline_status=EncounterPipelineStatus.GENERATION_FAILED,
    )
    db.add_all([ok, failed_a, failed_b])
    db.commit()

    response = client.get("/api/v1/encounters/failed", headers=_auth(doctor))

    assert response.status_code == 200
    returned_ids = {row["id"] for row in response.json()}
    assert returned_ids == {failed_a.id, failed_b.id}


# --- POST /encounters/{id}/retry -------------------------------------------


def test_retry_404s_for_a_missing_encounter(db, client):
    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    response = client.post("/api/v1/encounters/does-not-exist/retry", headers=_auth(doctor))
    assert response.status_code == 404


def test_retry_409s_when_not_actually_failed(db, client):
    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    encounter = Encounter(
        clinician_id=doctor.id, upload_idempotency_key="idem-not-failed", pipeline_status=EncounterPipelineStatus.TRANSCRIBED
    )
    db.add(encounter)
    db.commit()

    response = client.post(f"/api/v1/encounters/{encounter.id}/retry", headers=_auth(doctor))
    assert response.status_code == 409


def test_retry_from_transcription_failed_reruns_the_full_pipeline(db, client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", calls.append)

    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    encounter = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key="idem-retry-transcribe",
        pipeline_status=EncounterPipelineStatus.TRANSCRIPTION_FAILED,
        retry_count=4,
        last_pipeline_error="ASR vendor unreachable",
    )
    db.add(encounter)
    db.commit()

    response = client.post(f"/api/v1/encounters/{encounter.id}/retry", headers=_auth(doctor))

    assert response.status_code == 200
    body = response.json()
    assert body["pipeline_status"] == "uploaded"
    assert body["retry_count"] == 0
    assert body["last_pipeline_error"] is None
    assert calls == [encounter.id]  # the whole chain re-runs — no transcript existed to reuse


def test_retry_from_generation_failed_reruns_only_note_generation(db, client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.tasks.pipeline.run_note_generation", calls.append)

    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    encounter = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key="idem-retry-generate",
        pipeline_status=EncounterPipelineStatus.GENERATION_FAILED,
        retry_count=4,
        last_pipeline_error="Anthropic API unreachable",
    )
    db.add(encounter)
    db.commit()

    response = client.post(f"/api/v1/encounters/{encounter.id}/retry", headers=_auth(doctor))

    assert response.status_code == 200
    body = response.json()
    assert body["pipeline_status"] == "transcribed"
    assert body["retry_count"] == 0
    assert calls == [encounter.id]  # only the note-generation stage re-runs, not a wasted re-transcription


# --- sweep_stuck_encounters: the failure mode dead-lettering can't catch --


def _old(minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def test_sweep_rekicks_an_uploaded_encounter_stuck_past_the_threshold(db, monkeypatch):
    calls = []
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", calls.append)

    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    stuck = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key="idem-stuck-uploaded",
        pipeline_status=EncounterPipelineStatus.UPLOADED,
        pipeline_updated_at=_old(60),
    )
    db.add(stuck)
    db.commit()

    count = sweep_stuck_encounters()

    assert count == 1
    assert calls == [stuck.id]


def test_sweep_rekicks_a_transcribed_encounter_via_note_generation_only(db, monkeypatch):
    calls = []
    monkeypatch.setattr("app.tasks.pipeline.run_note_generation", calls.append)
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", lambda encounter_id: (_ for _ in ()).throw(AssertionError()))

    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    stuck = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key="idem-stuck-transcribed",
        pipeline_status=EncounterPipelineStatus.TRANSCRIBED,
        pipeline_updated_at=_old(60),
    )
    db.add(stuck)
    db.commit()

    count = sweep_stuck_encounters()

    assert count == 1
    assert calls == [stuck.id]


def test_sweep_leaves_a_recently_updated_encounter_alone(db, monkeypatch):
    calls = []
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", calls.append)

    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    fresh = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key="idem-fresh",
        pipeline_status=EncounterPipelineStatus.UPLOADED,
        pipeline_updated_at=_old(1),  # well inside the 30-minute default threshold
    )
    db.add(fresh)
    db.commit()

    count = sweep_stuck_encounters()

    assert count == 0
    assert calls == []


def test_sweep_never_touches_terminal_statuses_no_matter_how_old(db, monkeypatch):
    calls = []
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", calls.append)
    monkeypatch.setattr("app.tasks.pipeline.run_note_generation", calls.append)

    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    terminal_statuses = [
        EncounterPipelineStatus.NOTE_GENERATED,
        EncounterPipelineStatus.TRANSCRIPTION_FAILED,
        EncounterPipelineStatus.GENERATION_FAILED,
        EncounterPipelineStatus.BLOCKED_NO_CONSENT,
    ]
    for i, status_value in enumerate(terminal_statuses):
        db.add(
            Encounter(
                clinician_id=doctor.id,
                upload_idempotency_key=f"idem-terminal-{i}",
                pipeline_status=status_value,
                pipeline_updated_at=_old(24 * 60),  # a full day old
            )
        )
    db.commit()

    count = sweep_stuck_encounters()

    assert count == 0
    assert calls == []


# --- Phase 2.4: the encounter-status read the upload queue polls ----------


def test_read_encounter_returns_pipeline_status(db, client):
    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    encounter = Encounter(
        clinician_id=doctor.id,
        upload_idempotency_key="idem-read",
        pipeline_status=EncounterPipelineStatus.TRANSCRIBED,
    )
    db.add(encounter)
    db.commit()

    response = client.get(f"/api/v1/encounters/{encounter.id}", headers=_auth(doctor))

    assert response.status_code == 200
    assert response.json()["pipeline_status"] == "transcribed"


def test_read_encounter_404s_when_missing(db, client):
    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    assert client.get("/api/v1/encounters/nope", headers=_auth(doctor)).status_code == 404


def test_literal_worklist_paths_are_not_swallowed_by_the_id_route(db, client):
    """Route-order regression guard.

    FastAPI matches in registration order, so `GET /{encounter_id}` declared
    before `/loose` or `/failed` would swallow both — `/encounters/loose`
    would resolve as encounter_id="loose" and 404. That failure is silent
    (the worklist just goes empty) and trivially reintroduced by tidying the
    route file, so it is asserted rather than trusted to convention.
    """
    doctor = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    for path in ("/api/v1/encounters/loose", "/api/v1/encounters/failed"):
        response = client.get(path, headers=_auth(doctor))
        assert response.status_code == 200, f"{path} was swallowed by the id route"
        assert isinstance(response.json(), list), f"{path} returned a single encounter, not a list"
