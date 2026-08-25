from __future__ import annotations

from app.core.config import get_settings
from app.services.asr.base import TranscriptSegment
from app.services.note_generation.base import GeneratedNote, NoteGenerator

# Phase 1.4 planning update: sole note generator now — see
# docs/decisions/0021. Previously lived in the now-deleted luna.py;
# moved here since nothing shares it anymore.
SYSTEM_PROMPT = """You are producing a clinical note from a diarized Taglish \
consultation transcript. Output four sections in this order: Assessment, \
Plan, Subjective, Objective. Preserve Filipino speech verbatim in quoted \
excerpts — never silently translate. Use hedged clinical language ("appears \
to", "reports") rather than flat certainty. Do not invent findings for \
silent or low-confidence audio windows — omit instead."""


class HaikuNoteGenerator(NoteGenerator):
    """The sole note generator as of the 2026-08-25 planning update
    (docs/decisions/0021) — Claude Haiku 4.5, not P0-4's originally
    stated "Luna primary, Haiku configured fallback." `LunaNoteGenerator`
    (GPT-5.6, committed to without a bake-off per the roadmap) is deleted,
    not kept dormant — see the ASR vendor swap in Phase 1.3 for the same
    reasoning: an unused alternative with no real distinguishing capability
    left is dead weight, not a safety net.

    The system prompt above encodes the four P0-4 behavioral requirements
    (section order, verbatim Filipino, hedging, silence suppression) so
    they're reviewable now, before the first real API call is wired.
    """

    def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Wire this once a key is provisioned; "
                "SYSTEM_PROMPT above is ready to send as-is."
            )
        # TODO: single fused call per P0-4 ("Single fused call"), passing
        # transcript text + word-level confidence, parsing the four
        # sections plus source_spans out of a structured response.
        raise NotImplementedError("Haiku call not yet wired — see TODO above.")
