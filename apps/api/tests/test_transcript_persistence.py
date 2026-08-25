"""Phase 1.2: transcript persistence — closing the two gaps the
checklist names directly: `transcribe_encounter` computed `segments`
and discarded them (`_ = segments`), and `generate_note` always passed
`transcript=[]`.
"""

import dataclasses

from sqlalchemy import text

from app.core.security import EncryptedJSON
from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.models.encounter import Encounter
from app.models.note import Note
from app.models.transcript import Transcript
from app.services.asr.base import TranscriptSegment, TranscriptWord
from app.services.note_generation.base import GeneratedNote, GeneratedSection
from app.services.transcripts import load_transcript, persist_transcript
from app.tasks.pipeline import generate_note, transcribe_encounter

_SAMPLE_SEGMENTS = [
    TranscriptSegment(
        speaker="speaker_0",
        words=[
            TranscriptWord(text="Ano", start_ms=0, end_ms=200, confidence=0.95, speaker="speaker_0"),
            TranscriptWord(text="po", start_ms=200, end_ms=350, confidence=0.91, speaker="speaker_0"),
            TranscriptWord(text="ang", start_ms=350, end_ms=500, confidence=0.88, speaker="speaker_0"),
        ],
    ),
    TranscriptSegment(
        speaker="speaker_1",
        words=[
            TranscriptWord(text="Masakit", start_ms=600, end_ms=900, confidence=0.99, speaker="speaker_1"),
            TranscriptWord(text="ulo", start_ms=900, end_ms=1100, confidence=0.97, speaker="speaker_1"),
        ],
    ),
]


def _seed_encounter(db) -> tuple[Encounter, Clinician]:
    clinician = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)

    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key="idem-transcript-1")
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


# --- the (de)serialization round trip ---------------------------------


def _with_ids(segments, ids):
    """`_SAMPLE_SEGMENTS` are constructed with `id=None` — persistence is
    what actually assigns "seg0", "seg1", ... (Phase 1.4), so a round
    trip is expected to add that, not preserve `None`.
    """
    return [dataclasses.replace(seg, id=i) for seg, i in zip(segments, ids, strict=True)]


def test_persist_and_load_round_trips_segments(db):
    encounter, _clinician = _seed_encounter(db)

    persist_transcript(db, encounter.id, provider_name="groq_whisper_large_v3", segments=_SAMPLE_SEGMENTS)
    loaded = load_transcript(db, encounter.id)

    assert loaded == _with_ids(_SAMPLE_SEGMENTS, ["seg0", "seg1"])
    assert loaded[0].text == "Ano po ang"  # TranscriptSegment.text survives reconstruction


def test_load_transcript_returns_empty_list_when_nothing_persisted(db):
    encounter, _clinician = _seed_encounter(db)
    assert load_transcript(db, encounter.id) == []


def test_persist_transcript_upserts_rather_than_duplicating(db):
    encounter, _clinician = _seed_encounter(db)

    persist_transcript(db, encounter.id, provider_name="groq_whisper_large_v3", segments=_SAMPLE_SEGMENTS[:1])
    persist_transcript(db, encounter.id, provider_name="groq_whisper_large_v3", segments=_SAMPLE_SEGMENTS)

    rows = db.query(Transcript).filter(Transcript.encounter_id == encounter.id).all()
    assert len(rows) == 1
    assert load_transcript(db, encounter.id) == _with_ids(_SAMPLE_SEGMENTS, ["seg0", "seg1"])


def test_each_segment_gets_a_stable_id(db):
    encounter, _clinician = _seed_encounter(db)
    row = persist_transcript(db, encounter.id, provider_name="groq_whisper_large_v3", segments=_SAMPLE_SEGMENTS)
    assert [seg["id"] for seg in row.segments] == ["seg0", "seg1"]


# --- the transcript is PHI: same encryption treatment as everything else --


def test_segments_column_is_not_stored_as_plaintext(db):
    encounter, _clinician = _seed_encounter(db)
    persist_transcript(db, encounter.id, provider_name="groq_whisper_large_v3", segments=_SAMPLE_SEGMENTS)

    raw = db.execute(text("SELECT segments FROM transcripts WHERE encounter_id = :id"), {"id": encounter.id}).scalar()

    assert "Masakit" not in raw  # the actual spoken word never appears in plaintext
    assert not raw.strip().startswith("[")  # not parseable as the JSON it logically is


def test_encrypted_json_round_trip_in_isolation():
    coltype = EncryptedJSON()
    original = [{"id": "seg0", "speaker": "speaker_0", "words": [{"text": "hi"}]}]
    encrypted = coltype.process_bind_param(original, dialect=None)
    assert encrypted != original
    assert coltype.process_result_value(encrypted, dialect=None) == original


def test_retention_expires_at_is_set(db):
    encounter, _clinician = _seed_encounter(db)
    row = persist_transcript(db, encounter.id, provider_name="groq_whisper_large_v3", segments=_SAMPLE_SEGMENTS)
    assert row.retention_expires_at is not None


# --- wired into the pipeline -------------------------------------------


class _FakeASRProvider:
    provider_name = "fake-asr"
    model_version = "fake-model-v1"

    def transcribe(self, audio_object_key: str) -> list[TranscriptSegment]:
        return _SAMPLE_SEGMENTS


def test_transcribe_encounter_persists_the_transcript(db, monkeypatch):
    monkeypatch.setattr("app.tasks.pipeline.get_asr_provider", lambda: _FakeASRProvider())

    encounter, _clinician = _seed_encounter(db)
    encounter.audio_object_key = "encounters/x/audio/y.m4a"
    db.add(encounter)
    db.commit()

    transcribe_encounter(encounter.id)

    transcript = db.query(Transcript).filter(Transcript.encounter_id == encounter.id).one()
    assert transcript.asr_provider == "fake-asr"
    assert transcript.asr_model_version == "fake-model-v1"
    assert load_transcript(db, encounter.id) == _with_ids(_SAMPLE_SEGMENTS, ["seg0", "seg1"])


class _RecordingNoteGenerator:
    """Captures what it was called with instead of actually generating
    anything — proves generate_note passes the *persisted* transcript,
    without needing a real LLM call (still stubbed pending API keys)."""

    last_transcript: list[TranscriptSegment] | None = None

    def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
        type(self).last_transcript = transcript
        empty = GeneratedSection(text="")
        return GeneratedNote(
            assessment=empty, plan=empty, subjective=empty, objective=empty, provider="fake", prompt_version="fake-v0"
        )


def test_generate_note_loads_the_persisted_transcript_not_empty(db, monkeypatch):
    monkeypatch.setattr("app.tasks.pipeline.get_note_generator", lambda: _RecordingNoteGenerator())

    encounter, _clinician = _seed_encounter(db)
    persist_transcript(db, encounter.id, provider_name="groq_whisper_large_v3", segments=_SAMPLE_SEGMENTS)

    generate_note(encounter.id)

    assert _RecordingNoteGenerator.last_transcript == _with_ids(_SAMPLE_SEGMENTS, ["seg0", "seg1"])
    note = db.query(Note).filter(Note.encounter_id == encounter.id).one()
    assert note.note_generator_provider == "fake"
