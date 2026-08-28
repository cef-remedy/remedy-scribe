"""Claude Haiku 4.5 — the **configured fallback** as of decision 0035.

Not the default any more: note generation moved to Groq
(`groq.py`) to consolidate onto the vendor that already receives every
consultation's audio and transcript (decision 0018). This provider is
kept rather than deleted, which is a deliberate departure from how
decision 0021 treated `LunaNoteGenerator` and 0018 treated ElevenLabs.
Those were removed on the principle that "an unused alternative with no
real distinguishing capability left is dead weight, not a safety net" —
and the test is exactly that clause. Haiku *does* have a distinguishing
capability now: Groq's free tier caps throughput at roughly 8,000 tokens
per minute, which a full 20-40 minute consultation transcript exceeds
outright, and Groq's BAA excludes free-tier usage. Until that is
resolved commercially, a second provider that has neither limit is a
real escape hatch, not dead weight. Select it with
`NOTE_GENERATOR_PROVIDER=haiku`.

Design choices worth knowing before reading `generate()`:

- **Structured output via a forced tool call**, not free-text parsing.
  `tool_choice` pins the model to exactly one tool, so the response is
  always the shape `_build_tool_schema()` describes. (Groq cannot do
  this — its structured-output mode and tool use are mutually exclusive —
  which is why the two providers differ in transport but not in schema.)
- **Suppression is mechanical, not a polite request.** Low-confidence
  words are replaced with a literal `[INAUDIBLE]` marker in the prompt
  *before* the model ever sees them — the model cannot smooth over a gap
  it was never shown.
- **Citations are transcript segment IDs, not character offsets.** Asking
  an LLM to count characters produces confident, wrong numbers.
  `text_start`/`text_end` are computed server-side, never claimed by the
  model.
- **Citations are verified, not trusted.** A cited ID that doesn't
  correspond to a real input segment is dropped before the note is saved.

The last three now live in `shared.py`, so both providers enforce them
identically — see that module's docstring for why that is a correctness
requirement rather than tidiness.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings
from app.services.asr.base import TranscriptSegment
from app.services.note_generation.base import (
    GeneratedNote,
    GeneratedSection,
    NoteGenerator,
)
from app.services.note_generation.shared import (
    SECTION_NAMES,
    SYSTEM_PROMPT_BODY,
    build_section,
    build_sections,
    format_transcript,
    section_schema,
)

ANTHROPIC_MESSAGES_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 2048

# Bump whenever SYSTEM_PROMPT or the tool schema meaningfully changes.
# Stored on every generated Note (Note.prompt_version) specifically so
# "did we change the prompt?" is answerable from the data itself when
# edit burden shifts, not from memory after the fact.
PROMPT_VERSION = "haiku-v1"

TOOL_NAME = "emit_note"

SYSTEM_PROMPT = (
    "You are producing a clinical note from a diarized Taglish consultation "
    "transcript. Call the emit_note tool with the four sections.\n\n" + SYSTEM_PROMPT_BODY
)

# Kept as module-level aliases because the existing test suite imports
# them by these names. That is worth preserving rather than renaming: the
# 16 tests written against this provider now run unchanged against the
# shared implementations, which makes them a regression guard proving the
# extraction into shared.py did not change behaviour.
_format_transcript = format_transcript
_section_schema = section_schema
_build_section = build_section


def _build_tool_schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Emit the structured Assessment/Plan/Subjective/Objective note.",
        "input_schema": {
            "type": "object",
            "properties": {name: section_schema() for name in SECTION_NAMES},
            "required": list(SECTION_NAMES),
        },
    }


def _extract_tool_input(response_json: dict[str, Any]) -> dict[str, Any]:
    """⚠️ The error path here deliberately says nothing about *what* came
    back.

    An earlier version interpolated the entire response
    (`f"...: {response_json!r}"`). `app/tasks/pipeline.py:_mark_stage_failure`
    writes `str(exc)[:500]` into `Encounter.last_pipeline_error` — a plain,
    **unencrypted** `String(500)` column whose stated safety argument is
    that pipeline exceptions are always vendor/infrastructure errors and
    "can never leak PHI the way a raw request/response log could". A
    response that failed to include the tool block usually contains the
    model's prose *about the consultation*, so that claim was false on this
    one path: generated clinical content would have been written to an
    unencrypted column. Report the structure, never the content.
    """
    blocks = response_json.get("content") or []
    for block in blocks:
        if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
            return block["input"]
    kinds = sorted({str(b.get("type")) for b in blocks})
    raise RuntimeError(
        f"Anthropic response did not include a '{TOOL_NAME}' tool_use block "
        f"(stop_reason={response_json.get('stop_reason')!r}, block types={kinds})."
    )


class HaikuNoteGenerator(NoteGenerator):
    """Claude Haiku 4.5. See the module docstring for why this is now the
    fallback rather than the default, and why it was kept when two earlier
    alternatives were deleted.
    """

    provider_name = "haiku"

    def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Provision one before this can run against real audio.")

        if not transcript:
            # Nothing to generate from — and nothing worth spending a
            # real API call to be told that. Every section suppressed,
            # honestly, at zero cost.
            empty = GeneratedSection(text="", spans=[], suppressed=True)
            return GeneratedNote(
                assessment=empty,
                plan=empty,
                subjective=empty,
                objective=empty,
                provider=self.provider_name,
                prompt_version=PROMPT_VERSION,
            )

        valid_segment_ids = {segment.id for segment in transcript if segment.id is not None}
        transcript_text = format_transcript(transcript, settings.note_generation_low_confidence_threshold)

        response = httpx.post(
            ANTHROPIC_MESSAGES_ENDPOINT,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": settings.anthropic_model,
                "max_tokens": MAX_TOKENS,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": transcript_text}],
                "tools": [_build_tool_schema()],
                "tool_choice": {"type": "tool", "name": TOOL_NAME},
            },
            # A single-fused-call clinical note is a slow-but-real
            # dependency, not an unreachable one — contrast storage.py's
            # short timeouts for a startup check against a possibly
            # nonexistent endpoint.
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
        )
        response.raise_for_status()

        sections = build_sections(_extract_tool_input(response.json()), valid_segment_ids)

        return GeneratedNote(
            assessment=sections["assessment"],
            plan=sections["plan"],
            subjective=sections["subjective"],
            objective=sections["objective"],
            provider=self.provider_name,
            prompt_version=PROMPT_VERSION,
        )
