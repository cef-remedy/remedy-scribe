from __future__ import annotations

from app.core.config import get_settings
from app.services.asr.base import ASRProvider, TranscriptSegment, TranscriptWord

SCRIBE_ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"


class ElevenLabsScribeProvider(ASRProvider):
    """P0-3: ElevenLabs Scribe v2, diarization enabled.

    The actual HTTP call is stubbed pending ELEVENLABS_API_KEY and the
    Legal BAA/DPA confirmation (roadmap Open Questions) — the response
    parsing shape below matches Scribe v2's documented diarized-word
    output so tasks/pipeline.py has a real contract to code against now.
    """

    def transcribe(self, audio_object_key: str) -> list[TranscriptSegment]:
        settings = get_settings()
        if not settings.elevenlabs_api_key:
            raise RuntimeError(
                "ELEVENLABS_API_KEY is not set. Blocked on Legal's BAA/DPA "
                "confirmation for the Scribe API (see remedy-scribe-roadmap.md, "
                "Open Questions) before this can be wired to real audio."
            )

        # TODO: stream audio_object_key from S3/MinIO and POST to Scribe v2
        # with diarize=true. Left unimplemented until the API key + BAA are
        # confirmed; httpx is already a declared dependency for this call.
        raise NotImplementedError("ElevenLabs Scribe v2 call not yet wired — see TODO above.")

    @staticmethod
    def _parse_response(payload: dict) -> list[TranscriptSegment]:
        """Documented shape: {"words": [{"text", "start", "end", "speaker_id",
        "confidence"}, ...]}. Kept as a pure function so it's unit-testable
        without a live API key.
        """
        by_speaker: dict[str, list[TranscriptWord]] = {}
        for w in payload.get("words", []):
            speaker = w["speaker_id"]
            by_speaker.setdefault(speaker, []).append(
                TranscriptWord(
                    text=w["text"],
                    start_ms=int(w["start"] * 1000),
                    end_ms=int(w["end"] * 1000),
                    confidence=w["confidence"],
                    speaker=speaker,
                )
            )
        return [TranscriptSegment(speaker=s, words=ws) for s, ws in by_speaker.items()]
