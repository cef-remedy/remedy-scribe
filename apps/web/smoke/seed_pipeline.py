"""Dev-only: stand in for the ASR and note-generation legs of the pipeline.

Phase 3's grounding UI does not depend on *how* a transcript was produced,
only that one exists with real word timings and that a note cites its segment
IDs. Those two vendor calls (Groq Whisper, Claude Haiku) need provisioned API
keys, and they are already verified for real in Phase 1.3 and 1.4. So when
keys are not available, this seeds a transcript and note directly against the
same database, using the same persistence path the pipeline uses, so
everything downstream — the real audio object in MinIO, presigned playback,
Range requests, span resolution, the whole browser UI — is still exercised
for real.

What is substituted is stated plainly rather than hidden: run the smoke test
without SEED_PIPELINE to exercise the true end-to-end path.

Deliberately lives under smoke/ and not in the app: this is a test fixture,
and a "seed a fake transcript" endpoint in production code would be a
backdoor, not a convenience.

Usage:
    python smoke/seed_pipeline.py <encounter_id>
Prints the note id on success.
"""

import json
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2] / "api"
sys.path.insert(0, str(API_ROOT))

from app.db.session import SessionLocal  # noqa: E402
from app.models.encounter import Encounter, EncounterPipelineStatus  # noqa: E402
from app.models.note import Note, NoteStatus  # noqa: E402
from app.services.asr.base import TranscriptSegment, TranscriptWord  # noqa: E402
from app.services.transcripts import persist_transcript  # noqa: E402

#: Timings sit inside the real recording's duration, so playing a cited
#: passage plays actual audio rather than seeking past the end of the file.
_TURNS = [
    ("speaker_0", "Magandang umaga po kumusta ang pakiramdam ninyo", 500),
    ("speaker_1", "Doktora tatlong araw na akong nilalagnat at umuubo", 3_000),
    ("speaker_1", "Masakit din ang dibdib ko kapag humihinga ako ng malalim", 5_500),
    ("speaker_0", "May plema ba at ano ang kulay nito", 8_000),
    ("speaker_1", "Opo madilaw na plema at hirap akong matulog sa gabi", 9_500),
    ("speaker_0", "Kailangan nating i-x-ray ang dibdib at bibigyan kita ng antibiotic", 11_500),
]


def _segments() -> list[TranscriptSegment]:
    segments = []
    for speaker, line, start_ms in _TURNS:
        tokens = line.split()
        per = 220
        words = [
            TranscriptWord(
                text=token,
                start_ms=start_ms + i * per,
                end_ms=start_ms + i * per + per - 20,
                confidence=0.93,
                speaker=speaker,
            )
            for i, token in enumerate(tokens)
        ]
        segments.append(TranscriptSegment(speaker=speaker, words=words))
    return segments


def _section(sentences: list[tuple[str, list[str]]]) -> tuple[str, list[dict]]:
    """Builds text + spans exactly the way haiku.py's `_build_section` does:
    sentences joined by a single space, each span recording its own offsets.
    Duplicating that convention here is the point — if it ever changes, the
    server's `spans_fit` check will reject this seed loudly instead of the
    smoke test silently passing against a stale shape.
    """
    parts: list[str] = []
    spans: list[dict] = []
    cursor = 0
    for text, segment_ids in sentences:
        if parts:
            cursor += 1
        start = cursor
        end = start + len(text)
        spans.append({"text_start": start, "text_end": end, "segment_ids": segment_ids})
        parts.append(text)
        cursor = end
    return " ".join(parts), spans


def main(encounter_id: str) -> int:
    db = SessionLocal()
    try:
        encounter = db.get(Encounter, encounter_id)
        if encounter is None:
            print("ERROR: no such encounter", file=sys.stderr)
            return 1
        if encounter.audio_object_key is None:
            print("ERROR: encounter has no uploaded audio object", file=sys.stderr)
            return 1

        persist_transcript(
            db,
            encounter_id,
            provider_name="seeded-for-smoke",
            model_version="n/a",
            segments=_segments(),
        )

        assessment, a_spans = _section(
            [
                ("Community-acquired pneumonia is likely, pending imaging.", ["seg1", "seg2"]),
                ("Productive cough with yellow sputum and pleuritic chest pain.", ["seg2", "seg4"]),
            ]
        )
        plan, p_spans = _section(
            [
                ("Chest radiograph today.", ["seg5"]),
                ("Start empiric oral antibiotics and review in three days.", ["seg5"]),
                ("Advised to return sooner if breathlessness worsens.", []),
            ]
        )
        subjective, s_spans = _section(
            [
                ("Three days of fever and cough.", ["seg1"]),
                ("Reports difficulty sleeping at night.", ["seg4"]),
            ]
        )

        source_spans = {
            "assessment": {"suppressed": False, "spans": a_spans},
            "plan": {"suppressed": False, "spans": p_spans},
            "subjective": {"suppressed": False, "spans": s_spans},
            # Nothing was examined aloud on the recording, so this section is
            # suppressed rather than invented — the same thing generation does.
            "objective": {"suppressed": True, "spans": []},
        }

        existing = db.query(Note).filter(Note.encounter_id == encounter_id).one_or_none()
        note = existing or Note(encounter_id=encounter_id)
        note.status = NoteStatus.GENERATED
        note.assessment = assessment
        note.plan = plan
        note.subjective = subjective
        note.objective = ""
        note.source_spans = json.dumps(source_spans)
        note.note_generator_provider = "haiku"
        note.prompt_version = "seeded-for-smoke"
        db.add(note)

        encounter.pipeline_status = EncounterPipelineStatus.NOTE_GENERATED
        encounter.last_pipeline_error = None
        encounter.retry_count = 0
        db.add(encounter)
        db.commit()
        db.refresh(note)
        print(note.id)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
