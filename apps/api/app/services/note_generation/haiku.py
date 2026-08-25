"""Claude Haiku 4.5 — the sole note generator (decision 0021: Luna
dropped, not kept dormant as a role-swapped fallback).

Design choices worth knowing before reading `generate()`:

- **Structured output via a forced tool call**, not free-text parsing
  (checklist's explicit ask). `tool_choice` pins the model to exactly one
  tool, so the response is always the shape `_build_tool_schema()`
  describes — no regex-scraping a chat completion.
- **Suppression is mechanical, not a polite request.** Low-confidence
  words are replaced with a literal `[INAUDIBLE]` marker in the prompt
  *before* the model ever sees them (`_format_transcript`) — the model
  cannot smooth over a gap it was never shown. Per-section `suppressed`
  is also a schema field the model must set explicitly, not an
  after-the-fact inference from empty text.
- **Citations are transcript segment IDs, not character offsets.**
  Asking an LLM to count characters produces confident, wrong numbers
  (the checklist's own phrase for exactly this failure). The model cites
  `segment_ids` (e.g. `"seg0"`) that were handed to it in the prompt;
  `text_start`/`text_end` — offsets into the *note's own text* — are
  computed server-side by concatenation, never claimed by the model.
- **Citations are verified, not trusted.** Any cited segment_id that
  doesn't correspond to a real input segment is dropped before the note
  is ever saved (`_build_section`) — a hallucinated citation is worse
  than an under-cited sentence, since it looks like evidence.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings
from app.services.asr.base import TranscriptSegment
from app.services.note_generation.base import GeneratedNote, GeneratedSection, NoteGenerator, SourceSpan

ANTHROPIC_MESSAGES_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 2048

# Bump whenever SYSTEM_PROMPT or the tool schema meaningfully changes.
# Stored on every generated Note (Note.prompt_version) specifically so
# "did we change the prompt?" is answerable from the data itself when
# edit burden shifts, not from memory after the fact.
PROMPT_VERSION = "haiku-v1"

SECTION_NAMES = ("assessment", "plan", "subjective", "objective")
TOOL_NAME = "emit_note"

SYSTEM_PROMPT = """You are producing a clinical note from a diarized Taglish \
consultation transcript. The transcript is given to you as a series of \
lines, each tagged with a stable ID like [seg3 | speaker_unknown], in \
chronological order. Some words are replaced with the literal marker \
[INAUDIBLE] — that word's transcription was too unreliable to trust; do \
not guess what it might have been, and do not paraphrase around it as if \
you know what was said.

Call the emit_note tool with exactly four sections, in this order: \
Assessment, Plan, Subjective, Objective. For each section:
- Write clinical sentences in hedged language ("appears to", "reports") \
rather than flat certainty.
- Preserve Filipino speech verbatim in quoted excerpts — never silently \
translate.
- For every sentence, cite the segment_ids of every transcript line it \
draws from. Only cite IDs that actually appear in the transcript you were \
given — never invent one.
- If a section has no reliable spoken content to support it (the relevant \
part of the consult was silent, entirely [INAUDIBLE], or never discussed), \
set that section's suppressed field to true and leave its sentences empty. \
Do not invent plausible-sounding content to fill a section that has \
nothing behind it — an empty, honest section is correct; a fabricated \
one is not."""


def _format_transcript(transcript: list[TranscriptSegment], low_confidence_threshold: float) -> str:
    """One line per segment: `[seg_id | speaker] word word [INAUDIBLE] ...`.
    Adjacent [INAUDIBLE] markers are collapsed to one — a run of unreliable
    words is one gap, not one marker per word lost.
    """
    lines: list[str] = []
    for segment in transcript:
        rendered_words: list[str] = []
        for word in segment.words:
            token = "[INAUDIBLE]" if word.confidence < low_confidence_threshold else word.text
            if token == "[INAUDIBLE]" and rendered_words and rendered_words[-1] == "[INAUDIBLE]":
                continue
            rendered_words.append(token)
        seg_id = segment.id or "seg?"
        lines.append(f"[{seg_id} | {segment.speaker}] {' '.join(rendered_words)}")
    return "\n".join(lines)


def _section_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "suppressed": {
                "type": "boolean",
                "description": (
                    "True if there is no reliable spoken content for this section — "
                    "leave `sentences` empty rather than inventing content."
                ),
            },
            "sentences": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "One clinical sentence: hedged language, verbatim Filipino preserved.",
                        },
                        "segment_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "IDs (e.g. 'seg0') of every transcript line this sentence draws from. "
                                "Never invent an ID that wasn't in the input."
                            ),
                        },
                    },
                    "required": ["text", "segment_ids"],
                },
            },
        },
        "required": ["suppressed", "sentences"],
    }


def _build_tool_schema() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Emit the structured Assessment/Plan/Subjective/Objective note.",
        "input_schema": {
            "type": "object",
            "properties": {name: _section_schema() for name in SECTION_NAMES},
            "required": list(SECTION_NAMES),
        },
    }


def _extract_tool_input(response_json: dict[str, Any]) -> dict[str, Any]:
    for block in response_json.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
            return block["input"]
    raise RuntimeError(f"Anthropic response did not include a '{TOOL_NAME}' tool_use block: {response_json!r}")


def _build_section(raw: dict[str, Any], valid_segment_ids: set[str]) -> GeneratedSection:
    if raw.get("suppressed"):
        # Suppression wins over content even if the model inconsistently
        # also supplied sentences — mechanical enforcement, not trust.
        return GeneratedSection(text="", spans=[], suppressed=True)

    parts: list[str] = []
    spans: list[SourceSpan] = []
    cursor = 0
    for sentence in raw.get("sentences", []):
        text = sentence.get("text") or ""
        if not text:
            continue

        # Verified, not trusted: a cited ID that isn't one of the
        # segments we actually sent is dropped, not kept as if it were
        # real evidence.
        cited_ids = [sid for sid in sentence.get("segment_ids", []) if sid in valid_segment_ids]

        if parts:
            cursor += 1  # the separator " ".join(parts) will insert
        start = cursor
        end = start + len(text)
        spans.append(SourceSpan(text_start=start, text_end=end, segment_ids=cited_ids))
        parts.append(text)
        cursor = end

    return GeneratedSection(text=" ".join(parts), spans=spans, suppressed=False)


class HaikuNoteGenerator(NoteGenerator):
    """The sole note generator as of the 2026-08-25 planning update
    (docs/decisions/0021) — Claude Haiku 4.5, not P0-4's originally
    stated "Luna primary, Haiku configured fallback." `LunaNoteGenerator`
    (GPT-5.6, committed to without a bake-off per the roadmap) is deleted,
    not kept dormant — see the ASR vendor swap in Phase 1.3 for the same
    reasoning: an unused alternative with no real distinguishing capability
    left is dead weight, not a safety net.
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
        transcript_text = _format_transcript(transcript, settings.note_generation_low_confidence_threshold)
        tool_schema = _build_tool_schema()

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
                "tools": [tool_schema],
                "tool_choice": {"type": "tool", "name": TOOL_NAME},
            },
            # A single-fused-call clinical note is a slow-but-real
            # dependency, not an unreachable one — contrast storage.py's
            # short timeouts for a startup check against a possibly
            # nonexistent endpoint.
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
        )
        response.raise_for_status()
        tool_input = _extract_tool_input(response.json())

        sections = {name: _build_section(tool_input.get(name, {}), valid_segment_ids) for name in SECTION_NAMES}

        return GeneratedNote(
            assessment=sections["assessment"],
            plan=sections["plan"],
            subjective=sections["subjective"],
            objective=sections["objective"],
            provider=self.provider_name,
            prompt_version=PROMPT_VERSION,
        )
