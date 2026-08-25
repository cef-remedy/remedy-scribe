from app.services.note_generation.base import GeneratedNote, GeneratedSection, NoteGenerator, SourceSpan
from app.services.note_generation.haiku import HaikuNoteGenerator


def get_note_generator() -> NoteGenerator:
    """Haiku is the sole provider as of the 2026-08-25 planning update
    (docs/decisions/0021) — NOTE_GENERATOR_PROVIDER's Literal type only
    accepts "haiku" now, so this has nothing left to branch on. Kept as
    a function, not inlined at the one call site (tasks/pipeline.py), so
    a second provider is still a new class + one `if` here, not a
    call-site change — the same swap-point shape this interface already
    had, just currently exercised by one option instead of two.
    """
    return HaikuNoteGenerator()


__all__ = [
    "GeneratedNote",
    "GeneratedSection",
    "NoteGenerator",
    "SourceSpan",
    "get_note_generator",
]
