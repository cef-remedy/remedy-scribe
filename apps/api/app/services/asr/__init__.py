from app.services.asr.base import ASRProvider, TranscriptSegment, TranscriptWord
from app.services.asr.groq_whisper import GroqWhisperProvider


def get_asr_provider() -> ASRProvider:
    # Phase 1.3: Groq-hosted Whisper large-v3, not the PRD's named
    # ElevenLabs Scribe v2 — see docs/decisions/0018. This factory is
    # still the swap point if that decision changes: a new class + one
    # line here, not a call-site change anywhere else.
    return GroqWhisperProvider()


__all__ = ["ASRProvider", "TranscriptSegment", "TranscriptWord", "get_asr_provider"]
