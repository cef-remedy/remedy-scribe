from app.services.asr.base import ASRProvider, TranscriptSegment, TranscriptWord
from app.services.asr.elevenlabs import ElevenLabsScribeProvider


def get_asr_provider() -> ASRProvider:
    # Only one ASR vendor is in the PRD today (ElevenLabs Scribe v2). This
    # factory exists anyway so a Speechmatics fallback — the roadmap's
    # named contingency if the ElevenLabs BAA/DPA question resolves
    # negatively — is a new class + one line here, not a call-site change.
    return ElevenLabsScribeProvider()


__all__ = ["ASRProvider", "TranscriptSegment", "TranscriptWord", "get_asr_provider"]
