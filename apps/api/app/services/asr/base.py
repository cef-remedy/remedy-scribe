from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TranscriptWord:
    text: str
    start_ms: int
    end_ms: int
    confidence: float  # P0-3: "word-level confidence is retained and passed to the note-generation step"
    speaker: str  # diarization label, e.g. "speaker_0"


@dataclass
class TranscriptSegment:
    speaker: str
    words: list[TranscriptWord]

    @property
    def text(self) -> str:
        # P0-3: transcript preserves Filipino speech verbatim — this is a
        # straight join of ASR output, no translation step anywhere.
        return " ".join(w.text for w in self.words)


class ASRProvider(ABC):
    """One method, one contract, so a provider swap (see get_asr_provider)
    never touches call sites — only tasks/pipeline.py calls this.
    """

    #: Phase 1.2: stored on Transcript.asr_provider so a persisted
    #: transcript records what produced it.
    provider_name: str = "unknown"

    #: Phase 1.3: stored on Transcript.asr_model_version. A property, not
    #: a class attribute, on implementations where the model is
    #: configurable (GroqWhisperProvider reads it from settings) rather
    #: than fixed per class.
    model_version: str = "unknown"

    @abstractmethod
    def transcribe(self, audio_object_key: str) -> list[TranscriptSegment]:
        """Fetch the audio from object storage and return diarized,
        word-timed, confidence-scored segments.
        """
        raise NotImplementedError
