from __future__ import annotations

from app.core.config import get_settings
from app.services.asr.base import TranscriptSegment
from app.services.note_generation.base import GeneratedNote, NoteGenerator
from app.services.note_generation.luna import SYSTEM_PROMPT


class HaikuNoteGenerator(NoteGenerator):
    """P0-4 configured fallback: "Claude Haiku 4.5 remains available as a
    configured fallback if Luna underperforms in practice." Selected via
    NOTE_GENERATOR_PROVIDER=haiku (app/core/config.py) — no code change,
    per the roadmap's stated mitigation for the no-bake-off risk.

    Reuses Luna's SYSTEM_PROMPT: the four P0-4 behavioral requirements
    (section order, verbatim Filipino, hedging, silence suppression) are
    provider-agnostic and shouldn't drift between the two prompts.
    """

    def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Wire this once a key is provisioned.")
        # TODO: single fused call to Claude Haiku 4.5 using SYSTEM_PROMPT,
        # mirroring LunaNoteGenerator's response parsing contract.
        raise NotImplementedError("Haiku call not yet wired — see TODO above.")
