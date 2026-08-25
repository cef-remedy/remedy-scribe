from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.services.asr.base import TranscriptSegment


@dataclass
class SourceSpan:
    """One reference from a generated note line back to the transcript
    passage it came from — P0-4 ("every generated line is traceable back
    to its source transcript passage") and P0-7 (grounding UI: tap a line,
    see the passage, play the audio at transcript_start_ms).
    """

    text_start: int
    text_end: int
    transcript_start_ms: int
    transcript_end_ms: int


@dataclass
class GeneratedSection:
    text: str
    spans: list[SourceSpan] = field(default_factory=list)
    suppressed: bool = False  # True over silent/low-confidence audio windows (P0-4)


@dataclass
class GeneratedNote:
    assessment: GeneratedSection
    plan: GeneratedSection
    subjective: GeneratedSection
    objective: GeneratedSection
    provider: str  # "luna" | "haiku" — stored on Note.note_generator_provider


class NoteGenerator(ABC):
    """P0-4: "Claude Haiku 4.5 remains available as a configured fallback
    if Luna underperforms in practice." This interface is what makes that
    a config flag (NOTE_GENERATOR_PROVIDER) instead of a rebuild — see
    get_note_generator() in __init__.py.
    """

    @abstractmethod
    def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
        """Produce an Assessment/Plan/Subjective/Objective note.

        Implementations must suppress (not fabricate) content over
        low-confidence or silent windows, and must default to hedged
        clinical language rather than flat certainty — both are explicit
        P0-4 requirements, not implementation details left to the model.
        """
        raise NotImplementedError
