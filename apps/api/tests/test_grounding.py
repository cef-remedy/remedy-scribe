"""Phase 3: the grounding UI's data path (P0-7).

This feature's whole job is proof, which makes a *confidently wrong* answer
worse than no answer. So most of what follows tests the honesty of the
resolution rather than the happy path: that stale offsets are reported as
stale instead of highlighting the wrong words, that a section the doctor has
rewritten is labelled rather than presented as the model's work, that audio
the database believes exists is checked before a play button is offered, and
that "we could not reach storage" never gets rounded up to "it was deleted."
"""

from datetime import date, datetime, timezone

import pytest

from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.clinician import Clinician
from app.models.consent import ConsentEventType, ConsentLedgerEntry
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.models.note import Note, NoteRevision, NoteStatus
from app.models.patient import Patient
from app.models.transcript import Transcript
from app.services.grounding import (
    AudioNotPlayableError,
    AudioState,
    TranscriptState,
    presign_playback_url,
    resolve_grounding,
    spans_fit_text,
)

# --- fixtures -------------------------------------------------------------

_ASSESSMENT = "Likely community-acquired pneumonia. Consider chest radiograph."
#: The two sentences above, as generation would have recorded them: offsets
#: into the section's own text, joined by the single space `_build_section`
#: inserts. Written out literally rather than computed, so a change to the
#: span convention breaks these tests loudly instead of adapting to itself.
_ASSESSMENT_SPANS = [
    {"text_start": 0, "text_end": 36, "segment_ids": ["seg2"]},
    {"text_start": 37, "text_end": 63, "segment_ids": ["seg4", "seg5"]},
]


def _words(texts: list[str], start_ms: int) -> list[dict]:
    words = []
    cursor = start_ms
    for text in texts:
        words.append(
            {
                "text": text,
                "start_ms": cursor,
                "end_ms": cursor + 400,
                "confidence": 0.95,
                "speaker": "speaker_0",
            }
        )
        cursor += 500
    return words


def _segments(count: int = 8) -> list[dict]:
    return [
        {
            "id": f"seg{i}",
            "speaker": f"speaker_{i % 2}",
            "words": _words([f"line{i}", "word", "here"], start_ms=i * 5_000),
        }
        for i in range(count)
    ]


def _doctor(db, email: str = "doc@example.com") -> Clinician:
    clinician = Clinician(email=email, full_name="Dr. Reyes", hashed_password="x", role="doctor")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician


def _auth(clinician: Clinician) -> dict:
    return {"Authorization": f"Bearer {create_access_token(subject=clinician.id, extra_claims={'role': clinician.role})}"}


def _scenario(
    db,
    *,
    assessment: str = _ASSESSMENT,
    spans: list[dict] | None = None,
    with_transcript: bool = True,
    audio_object_key: str | None = "encounters/e1/audio/abc.weba",
    audio_deleted_at: datetime | None = None,
    segment_count: int = 8,
) -> tuple[Encounter, Note]:
    import json

    clinician = _doctor(db)
    patient = Patient(full_name="Maria Santos", birthdate=date(1988, 4, 12))
    db.add(patient)
    db.commit()
    db.refresh(patient)

    encounter = Encounter(
        patient_id=patient.id,
        clinician_id=clinician.id,
        upload_idempotency_key="idem-ground-1",
        audio_object_key=audio_object_key,
        audio_deleted_at=audio_deleted_at,
        pipeline_status=EncounterPipelineStatus.NOTE_GENERATED,
    )
    db.add(encounter)
    db.commit()
    db.refresh(encounter)

    if with_transcript:
        db.add(
            Transcript(
                encounter_id=encounter.id,
                asr_provider="groq-whisper",
                asr_model_version="whisper-large-v3",
                segments=_segments(segment_count),
            )
        )

    source_spans = {
        "assessment": {"suppressed": False, "spans": _ASSESSMENT_SPANS if spans is None else spans},
        "plan": {"suppressed": False, "spans": []},
        "subjective": {"suppressed": True, "spans": []},
        "objective": {"suppressed": False, "spans": []},
    }
    note = Note(
        encounter_id=encounter.id,
        status=NoteStatus.GENERATED,
        assessment=assessment,
        plan="",
        subjective="",
        objective="",
        source_spans=json.dumps(source_spans),
        note_generator_provider="haiku",
        prompt_version="v1",
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return encounter, note


@pytest.fixture()
def audio_present(monkeypatch):
    monkeypatch.setattr("app.services.storage.head_object", lambda key: {"ContentLength": 12345})


# --- spans_fit_text: the offsets are checked, not trusted ------------------


def test_spans_fit_the_text_they_were_generated_from():
    assert spans_fit_text(_ASSESSMENT, _ASSESSMENT_SPANS) is True


def test_an_insertion_makes_the_stored_offsets_stop_fitting():
    """The failure this prevents: a doctor adds a word to the first sentence,
    every later offset shifts, and highlighting by stored offsets would point
    at the wrong words while looking perfectly confident.
    """
    edited = _ASSESSMENT.replace("Likely", "Likely severe")
    assert spans_fit_text(edited, _ASSESSMENT_SPANS) is False


def test_a_deletion_makes_the_stored_offsets_stop_fitting():
    assert spans_fit_text(_ASSESSMENT[:-10], _ASSESSMENT_SPANS) is False


def test_a_same_length_substitution_still_fits():
    """Correctly so: the offsets genuinely still delimit that sentence.
    Whether its *content* is still the model's is a different question, and
    `edited_since_generation` is what answers it.
    """
    edited = _ASSESSMENT.replace("Likely", "Likelu")  # same length
    assert spans_fit_text(edited, _ASSESSMENT_SPANS) is True


def test_no_spans_never_counts_as_fitting():
    assert spans_fit_text("anything", []) is False


def test_out_of_range_offsets_do_not_fit():
    assert spans_fit_text("short", [{"text_start": 0, "text_end": 900, "segment_ids": []}]) is False


# --- resolution: what the UI is handed ------------------------------------


def test_spans_resolve_to_cited_passages_with_timestamps(db, audio_present):
    _, note = _scenario(db)

    grounding = resolve_grounding(db, note)
    assessment = grounding.sections["assessment"]

    assert assessment.spans_fit is True
    assert [s.segment_ids for s in assessment.spans] == [["seg2"], ["seg4", "seg5"]]
    # The span carries the note's own words, resolved server-side, so the
    # client never re-slices by offset and gets it subtly wrong.
    assert assessment.spans[0].text == "Likely community-acquired pneumonia."

    cited = {s.id: s for s in grounding.segments if s.cited}
    assert set(cited) == {"seg2", "seg4", "seg5"}
    assert cited["seg2"].start_ms == 10_000
    assert cited["seg2"].end_ms == 11_400


def test_neighbouring_passages_come_along_but_are_not_marked_as_evidence(db, audio_present):
    """Context matters — a passage read without its surroundings is easy to
    misread — but a neighbour is not what the note line cited, and the
    response has to say so or the UI will present it as proof.
    """
    _, note = _scenario(db)

    grounding = resolve_grounding(db, note)
    by_id = {s.id: s for s in grounding.segments}

    assert by_id["seg1"].cited is False
    assert by_id["seg3"].cited is False
    assert by_id["seg2"].cited is True


def test_the_whole_transcript_is_not_returned(db, audio_present):
    """PHI minimisation: the transcript is verbatim, including what the doctor
    chose not to write down. Rendering a highlight does not require shipping
    all of it to a browser.
    """
    _, note = _scenario(db, segment_count=40)

    grounding = resolve_grounding(db, note)

    returned = {s.id for s in grounding.segments}
    assert returned == {"seg1", "seg2", "seg3", "seg4", "seg5", "seg6"}
    assert "seg20" not in returned


def test_a_line_that_cites_nothing_is_surfaced_rather_than_hidden(db, audio_present):
    """A generated line with no citation is exactly the line a doctor should
    scrutinise. Dropping the span would render it as ordinary prose.
    """
    _, note = _scenario(
        db,
        spans=[
            {"text_start": 0, "text_end": 36, "segment_ids": []},
            {"text_start": 37, "text_end": 63, "segment_ids": ["seg4"]},
        ],
    )

    grounding = resolve_grounding(db, note)
    spans = grounding.sections["assessment"].spans

    assert len(spans) == 2
    assert spans[0].segment_ids == []


def test_a_suppressed_section_is_reported_as_suppressed(db, audio_present):
    _, note = _scenario(db)

    grounding = resolve_grounding(db, note)

    assert grounding.sections["subjective"].suppressed is True
    assert grounding.sections["subjective"].spans == []


def test_an_edit_that_shifts_offsets_disables_highlighting(db, audio_present):
    """End to end through the resolver: stale offsets produce spans with no
    resolved text and spans_fit False, so a client following the contract
    cannot highlight the wrong words.
    """
    _, note = _scenario(db, assessment=_ASSESSMENT.replace("Likely", "Likely severe"))

    section = resolve_grounding(db, note).sections["assessment"]

    assert section.spans_fit is False
    assert all(span.text == "" for span in section.spans)


def test_an_edited_section_is_labelled_even_when_the_offsets_still_fit(db, audio_present):
    """The subtle one. A same-length rewrite leaves the offsets structurally
    valid, so `spans_fit` stays True — but the words are now the doctor's, and
    presenting a transcript passage as "the source of this line" would be
    false. Two flags, because they answer two different questions.
    """
    _, note = _scenario(db)
    clinician = db.query(Clinician).one()
    db.add(
        NoteRevision(
            note_id=note.id,
            section="assessment",
            previous_text=_ASSESSMENT,
            new_text=_ASSESSMENT,
            edited_by_clinician_id=clinician.id,
        )
    )
    db.commit()

    section = resolve_grounding(db, note).sections["assessment"]

    assert section.spans_fit is True
    assert section.edited_since_generation is True
    assert resolve_grounding(db, note).sections["plan"].edited_since_generation is False


def test_a_passage_with_no_words_disables_its_own_playback_not_the_screen(db, audio_present):
    """A diarized turn with no words should not exist, but the persisted JSON
    permits one, and `words[0]` on an empty list is a 500 on a read endpoint.
    """
    encounter, note = _scenario(db)
    transcript = db.query(Transcript).one()
    segments = list(transcript.segments)
    segments[2] = {"id": "seg2", "speaker": "speaker_0", "words": []}
    transcript.segments = segments
    db.add(transcript)
    db.commit()

    grounding = resolve_grounding(db, note)
    seg2 = next(s for s in grounding.segments if s.id == "seg2")

    assert seg2.start_ms is None
    assert seg2.end_ms is None
    assert seg2.cited is True


def test_unparseable_source_spans_do_not_break_the_read(db, audio_present):
    _, note = _scenario(db)
    note.source_spans = "{not json"
    db.add(note)
    db.commit()

    grounding = resolve_grounding(db, note)

    assert grounding.sections["assessment"].spans == []
    assert grounding.sections["assessment"].spans_fit is False


# --- the degradation ladder: notes outlive audio --------------------------


def test_transcript_and_audio_both_present_is_the_top_rung(db, audio_present):
    _, note = _scenario(db)

    grounding = resolve_grounding(db, note)

    assert grounding.audio_state is AudioState.AVAILABLE
    assert grounding.transcript_state is TranscriptState.AVAILABLE


def test_a_missing_transcript_under_an_existing_note_is_a_deletion_not_a_pending_pipeline(db, audio_present):
    """A note exists, so generation ran, so a transcript existed. Its absence
    now is a permanent loss of the source — a different thing to tell a doctor
    than "transcription hasn't finished."
    """
    _, note = _scenario(db, with_transcript=False)

    grounding = resolve_grounding(db, note)

    assert grounding.transcript_state is TranscriptState.EXPIRED
    assert grounding.segments == []


def test_a_withdrawn_transcript_says_withdrawn_rather_than_expired(db, audio_present):
    """Phase 4.4's retention purge deletes a withdrawn encounter's transcript
    alongside its audio. Reporting that as "the retention period elapsed"
    gives the doctor the wrong reason — the exact mistake decision 0030 built
    the five-state audio ladder to avoid, repeated one ladder over.
    """
    encounter, note = _scenario(db, with_transcript=False)
    db.add(
        ConsentLedgerEntry(
            encounter_id=encounter.id,
            event=ConsentEventType.WITHDRAWN,
            participant_roster="[]",
            purposes="[]",
            script_language="en",
        )
    )
    db.commit()

    assert resolve_grounding(db, note).transcript_state is TranscriptState.WITHDRAWN


def test_a_deleted_transcript_with_no_withdrawal_is_still_expiry(db, audio_present):
    _, note = _scenario(db, with_transcript=False)

    assert resolve_grounding(db, note).transcript_state is TranscriptState.EXPIRED


def test_audio_never_uploaded_is_distinguished_from_audio_deleted(db):
    _, note = _scenario(db, audio_object_key=None)

    assert resolve_grounding(db, note).audio_state is AudioState.NEVER_RECORDED


def test_withdrawal_is_reported_as_withdrawal_not_as_expiry(db):
    """Observably identical to retention expiry — no object either way — but
    the reason is what a doctor needs. The recording is gone because someone
    asked, not because time passed.
    """
    encounter, note = _scenario(db, audio_deleted_at=datetime.now(timezone.utc))
    db.add(
        ConsentLedgerEntry(
            encounter_id=encounter.id,
            event=ConsentEventType.WITHDRAWN,
            participant_roster="[]",
            purposes="[]",
            script_language="en",
        )
    )
    db.commit()

    assert resolve_grounding(db, note).audio_state is AudioState.WITHDRAWN


def test_deleted_audio_with_no_withdrawal_on_record_is_expiry(db):
    _, note = _scenario(db, audio_deleted_at=datetime.now(timezone.utc))

    assert resolve_grounding(db, note).audio_state is AudioState.EXPIRED


def test_the_database_claiming_audio_exists_is_not_evidence(db, monkeypatch):
    """The trap this phase's heads-up is about. The bucket's own lifecycle rule
    expires objects after audio_retention_days and nothing writes back to the
    encounter row, so `audio_object_key` set with `audio_deleted_at` NULL does
    not mean the bytes are there. Trusting the row is how a doctor gets a play
    button that does nothing and an opaque S3 404.
    """
    encounter, note = _scenario(db)
    monkeypatch.setattr("app.services.storage.head_object", lambda key: None)

    grounding = resolve_grounding(db, note)

    assert grounding.audio_state is AudioState.EXPIRED
    # And the row is corrected, so every later reader agrees and the HEAD is
    # paid once rather than on every note open.
    db.refresh(encounter)
    assert encounter.audio_deleted_at is not None


def test_unreachable_storage_is_not_rounded_up_to_deleted(db, monkeypatch):
    """"We could not check" and "it is gone" warrant different words to a
    doctor, and one of them is permanent. Guessing the harsher one is still a
    guess — and it would also stamp a deletion that never happened.
    """

    def _boom(key):
        raise RuntimeError("connection refused")

    encounter, note = _scenario(db)
    monkeypatch.setattr("app.services.storage.head_object", _boom)

    grounding = resolve_grounding(db, note)

    assert grounding.audio_state is AudioState.UNREACHABLE
    db.refresh(encounter)
    assert encounter.audio_deleted_at is None


def test_grounding_still_resolves_when_audio_is_gone(db):
    """The middle rung of the ladder: transcript-only grounding is still
    useful, so losing the audio must not take the highlighting with it.
    """
    _, note = _scenario(db, audio_deleted_at=datetime.now(timezone.utc))

    grounding = resolve_grounding(db, note)

    assert grounding.audio_state is AudioState.EXPIRED
    assert grounding.transcript_state is TranscriptState.AVAILABLE
    assert grounding.sections["assessment"].spans_fit is True


# --- presigned playback ---------------------------------------------------


def test_playback_url_is_signed_with_no_store_and_inline_disposition(db, monkeypatch, audio_present):
    """P0-7 asks for playback "without permanently re-downloading PHI". The
    signed response headers are how that is enforced at the storage layer
    rather than hoped for in the client: no-store keeps the audio out of the
    browser's HTTP cache, and inline keeps it out of the Downloads folder.
    """
    captured: dict = {}

    class _FakeClient:
        def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803 - boto3's own casing
            captured.update({"op": op, "params": Params, "expires": ExpiresIn})
            return "https://example.invalid/presigned"

    monkeypatch.setattr("app.services.storage._client", lambda: _FakeClient())
    encounter, _ = _scenario(db)

    url, expires_in = presign_playback_url(db, encounter)

    assert url == "https://example.invalid/presigned"
    assert captured["op"] == "get_object"
    assert captured["params"]["ResponseCacheControl"] == "no-store"
    assert captured["params"]["ResponseContentDisposition"] == "inline"
    # Shorter than the 900s part-upload window: this one is a playable handle
    # on PHI, not a URL handed to a device that already holds the bytes.
    assert expires_in == 300
    assert captured["expires"] == 300


def test_no_url_is_minted_for_audio_that_is_gone(db):
    encounter, _ = _scenario(db, audio_deleted_at=datetime.now(timezone.utc))

    with pytest.raises(AudioNotPlayableError) as exc:
        presign_playback_url(db, encounter)

    assert exc.value.state is AudioState.EXPIRED
    # The reason travels with the refusal — a bare "no audio" is the dead play
    # button this phase exists to avoid.
    assert "retention" in str(exc.value).lower()


# --- routes ---------------------------------------------------------------


def test_grounding_endpoint_returns_the_resolved_view(db, client, audio_present):
    _, note = _scenario(db)
    clinician = db.query(Clinician).one()

    response = client.get(f"/api/v1/notes/{note.id}/grounding", headers=_auth(clinician))

    assert response.status_code == 200
    body = response.json()
    assert body["note_id"] == note.id
    assert body["audio_state"] == "available"
    assert body["transcript_state"] == "available"
    assert body["sections"]["assessment"]["spans_fit"] is True
    assert body["sections"]["assessment"]["spans"][0]["segment_ids"] == ["seg2"]


def test_grounding_is_audited_separately_from_reading_the_note(db, client, audio_present):
    """Reading a note returns the clinician-facing summary; this returns
    verbatim transcript passages. A strictly larger disclosure deserves its
    own accountable action rather than hiding inside note.read.
    """
    _, note = _scenario(db)
    clinician = db.query(Clinician).one()

    client.get(f"/api/v1/notes/{note.id}/grounding", headers=_auth(clinician))

    actions = [row.action for row in db.query(AuditLog).all()]
    assert "note.grounding.read" in actions


def test_grounding_for_an_unknown_note_is_404(db, client):
    clinician = _doctor(db)

    response = client.get("/api/v1/notes/does-not-exist/grounding", headers=_auth(clinician))

    assert response.status_code == 404


def test_audio_url_endpoint_returns_a_short_lived_url(db, client, monkeypatch, audio_present):
    monkeypatch.setattr(
        "app.services.storage.presign_audio_playback",
        lambda key, expires_in=None: ("https://example.invalid/audio", 300),
    )
    encounter, _ = _scenario(db)
    clinician = db.query(Clinician).one()

    response = client.get(f"/api/v1/encounters/{encounter.id}/audio-url", headers=_auth(clinician))

    assert response.status_code == 200
    assert response.json() == {"url": "https://example.invalid/audio", "expires_in_seconds": 300}


def test_playing_audio_is_audited_without_recording_the_object_key(db, client, monkeypatch, audio_present):
    """The key is a direct pointer to the bytes, and an audit row outlives the
    retention window of what it points at.
    """
    monkeypatch.setattr(
        "app.services.storage.presign_audio_playback",
        lambda key, expires_in=None: ("https://example.invalid/audio", 300),
    )
    encounter, _ = _scenario(db)
    clinician = db.query(Clinician).one()

    client.get(f"/api/v1/encounters/{encounter.id}/audio-url", headers=_auth(clinician))

    rows = [r for r in db.query(AuditLog).all() if r.action == "encounter.audio.playback_url"]
    assert len(rows) == 1
    assert rows[0].entity_id == encounter.id
    assert rows[0].diff is None


def test_audio_url_for_deleted_audio_is_409_with_the_reason(db, client):
    """409, not 404: the encounter exists and the caller may read it. What is
    missing is the recording — a state problem, and the doctor is told which
    state.
    """
    encounter, _ = _scenario(db, audio_deleted_at=datetime.now(timezone.utc))
    db.add(
        ConsentLedgerEntry(
            encounter_id=encounter.id,
            event=ConsentEventType.WITHDRAWN,
            participant_roster="[]",
            purposes="[]",
            script_language="en",
        )
    )
    db.commit()
    clinician = db.query(Clinician).one()

    response = client.get(f"/api/v1/encounters/{encounter.id}/audio-url", headers=_auth(clinician))

    assert response.status_code == 409
    assert "patient's request" in response.json()["detail"]


def test_audio_url_for_an_unknown_encounter_is_404(db, client):
    clinician = _doctor(db)

    response = client.get("/api/v1/encounters/does-not-exist/audio-url", headers=_auth(clinician))

    assert response.status_code == 404


def test_the_audio_url_route_does_not_shadow_the_worklist_routes(db, client):
    """Phase 2.5 learned this the hard way: FastAPI matches in registration
    order, and a two-segment path parameter added carelessly can swallow a
    literal sibling. `/encounters/loose` must still be the loose-sessions tray.
    """
    clinician = _doctor(db)

    assert client.get("/api/v1/encounters/loose", headers=_auth(clinician)).status_code == 200
    assert client.get("/api/v1/encounters/failed", headers=_auth(clinician)).status_code == 200
