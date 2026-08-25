from __future__ import annotations

from app.core.config import get_settings
from app.services.asr.base import TranscriptSegment
from app.services.note_generation.base import GeneratedNote, NoteGenerator

SYSTEM_PROMPT = """You are producing a clinical note from a diarized Taglish \
consultation transcript. Output four sections in this order: Assessment, \
Plan, Subjective, Objective. Preserve Filipino speech verbatim in quoted \
excerpts — never silently translate. Use hedged clinical language ("appears \
to", "reports") rather than flat certainty. Do not invent findings for \
silent or low-confidence audio windows — omit instead."""


class LunaNoteGenerator(NoteGenerator):
    """P0-4 primary provider: "Single fused call using GPT-5.6 Luna,
    committed to directly without a formal vendor bake-off."

    The HTTP call is stubbed pending OPENAI_API_KEY — Luna was committed
    to without a bake-off (roadmap: "no practical way to source enough
    consented test recordings"), so there's no real transcript to call it
    against yet. The system prompt above already encodes the four P0-4
    behavioral requirements (section order, verbatim Filipino, hedging,
    silence suppression) so they're reviewable now, before the first
    real API call is wired.
    """

    def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
        settings = get_settings()
        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Wire this once a key is provisioned; "
                "SYSTEM_PROMPT above is ready to send as-is."
            )
        # TODO: single fused call per P0-4 ("Single fused call"), passing
        # transcript text + word-level confidence, parsing the four
        # sections plus source_spans out of a structured response.
        raise NotImplementedError("Luna call not yet wired — see TODO above.")
