from app.core.config import get_settings
from app.services.note_generation.base import GeneratedNote, GeneratedSection, NoteGenerator, SourceSpan
from app.services.note_generation.haiku import HaikuNoteGenerator
from app.services.note_generation.luna import LunaNoteGenerator


def get_note_generator() -> NoteGenerator:
    """The swap point for P0-4's Luna-primary/Haiku-fallback requirement:
    flip NOTE_GENERATOR_PROVIDER in config and every call site downstream
    (tasks/pipeline.py) picks it up with no code change.
    """
    provider = get_settings().note_generator_provider
    if provider == "haiku":
        return HaikuNoteGenerator()
    return LunaNoteGenerator()


__all__ = [
    "GeneratedNote",
    "GeneratedSection",
    "NoteGenerator",
    "SourceSpan",
    "get_note_generator",
]
