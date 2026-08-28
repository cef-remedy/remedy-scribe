from app.core.config import get_settings
from app.services.note_generation.base import (
    GeneratedNote,
    GeneratedSection,
    NoteGenerator,
    SourceSpan,
)
from app.services.note_generation.groq import GroqNoteGenerator
from app.services.note_generation.haiku import HaikuNoteGenerator


def get_note_generator() -> NoteGenerator:
    """The provider swap point, now genuinely exercised by two options.

    Groq is the default as of decision 0035 — note generation moved to the
    vendor that already receives every consultation's audio and transcript
    (decision 0018), so the clinic contracts with, audits and discloses one
    processor rather than two.

    Haiku is kept selectable rather than deleted. That is the opposite of
    what decision 0021 did to `LunaNoteGenerator` and 0018 did to
    ElevenLabs, and the reason is the principle those decisions actually
    stated: an alternative is dead weight only when it has "no real
    distinguishing capability left". Haiku has one — Groq's free tier caps
    throughput below what a full consultation transcript needs, and its
    BAA excludes free-tier use. Until that is settled commercially, one
    environment variable is the difference between a blocked pilot and a
    working one.
    """
    if get_settings().note_generator_provider == "haiku":
        return HaikuNoteGenerator()
    return GroqNoteGenerator()


__all__ = [
    "GeneratedNote",
    "GeneratedSection",
    "GroqNoteGenerator",
    "HaikuNoteGenerator",
    "NoteGenerator",
    "SourceSpan",
    "get_note_generator",
]
