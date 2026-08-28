"""Groq-hosted note generation — the default provider as of decision 0035,
replacing Anthropic Haiku 4.5.

The change is a **vendor consolidation**, not just a cost one. Phase 1.3
already sends every consultation's audio to Groq for transcription
(decision 0018), so the recording and its verbatim transcript are already
processed there. Moving note generation to the same vendor means one
processor to contract with, audit and disclose under the Data Privacy
Act, instead of two — and the note is derived from a transcript Groq has
already seen, so it discloses nothing new.

## Why this does not use forced tool use

Haiku pinned the response shape with `tool_choice`. Groq's OpenAI-
compatible API can do that too, but its docs are explicit that
**"Streaming and tool use are not currently supported with Structured
Outputs"** — the two mechanisms are mutually exclusive there.

Given the choice, `response_format: json_schema` with `strict: true` is
the stronger one: the schema is enforced by constrained decoding, so the
model *cannot* emit a token sequence that violates it, whereas a forced
tool call still asks a model to fill a schema correctly. The trade is
that arguments arrive as a JSON **string** to parse rather than a
pre-parsed object, which adds a failure mode (`_parse_payload` below).

Everything clinically load-bearing — the prompt, the schema, the
mechanical `[INAUDIBLE]` suppression, server-side offsets, and citation
verification — lives in `shared.py` and is byte-identical to the
Anthropic path. Only the transport differs.

## Two operational limits, stated because they are not obvious

- **Free-tier throughput does not fit a real consultation.** Groq's free
  tier allows ~8,000 tokens per minute on this model, while a 20-40
  minute consultation transcript is comfortably 10k-20k tokens. A full
  consult will exceed the per-minute allowance on a free key. Short
  visits will work; a real clinic day will not. See decision 0035.
- **PHI needs a paid plan and a Production-status model.** Groq's BAA
  covers "Covered Cloud Services", which excludes both preview-stage
  models and services provided free of charge. `openai/gpt-oss-120b` is
  the default here specifically because it is Production status; a
  preview model would be cheaper and is *not* an option for PHI.
"""

from __future__ import annotations

import json
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
    build_sections,
    format_transcript,
    note_schema,
)

GROQ_CHAT_COMPLETIONS_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
SCHEMA_NAME = "clinical_note"

# Sections are prose, not reasoning transcripts, but gpt-oss-class models
# spend tokens thinking before answering and that spend counts here.
MAX_TOKENS = 8192

# Bump whenever SYSTEM_PROMPT or the schema meaningfully changes. Stored on
# every generated Note (Note.prompt_version) precisely so "did we change
# the prompt?" is answerable from the data when edit burden shifts, rather
# than from memory. The provider name is in the value because the two
# providers' prompts differ in their first sentence and are versioned
# independently.
PROMPT_VERSION = "groq-v1"

SYSTEM_PROMPT = (
    "You are producing a clinical note from a diarized Taglish consultation "
    "transcript. Return a single JSON object matching the provided schema "
    "exactly — no prose outside it.\n\n" + SYSTEM_PROMPT_BODY
)


class GroqNoteParseError(RuntimeError):
    """Groq returned something that is not the agreed schema.

    ⚠️ **This exception's message must never contain model output.**
    `app/tasks/pipeline.py:_mark_stage_failure` writes `str(exc)[:500]`
    into `Encounter.last_pipeline_error`, which is a plain unencrypted
    `String(500)` column — its whole safety argument (see the model's own
    comment) is that pipeline exceptions are vendor/infrastructure errors
    that never contain transcript or note content. A parse error is the
    one failure where the tempting debug detail *is* the note text, so it
    is deliberately described structurally instead. The same trap existed
    on the Anthropic path and is fixed there too.
    """


def _parse_payload(response_json: dict[str, Any]) -> dict[str, Any]:
    """Pull the schema-constrained JSON object out of a chat completion.

    With `strict: true` the content should always parse. "Should" is not
    a guarantee worth betting a pipeline run on: a truncated response
    (hitting `max_tokens` mid-object) produces a valid HTTP 200 carrying
    invalid JSON, which is exactly the shape of failure that reads as a
    mysterious 500 later if it isn't named here.
    """
    choices = response_json.get("choices") or []
    if not choices:
        raise GroqNoteParseError("Groq returned no choices for the note request.")

    message = choices[0].get("message") or {}
    if message.get("refusal"):
        # A refusal is a real, reportable outcome rather than a crash —
        # and its text is the model's, so it is not repeated here.
        raise GroqNoteParseError("Groq declined to generate a note for this transcript.")

    content = message.get("content")
    if not content:
        finish_reason = choices[0].get("finish_reason")
        raise GroqNoteParseError(f"Groq returned an empty note body (finish_reason={finish_reason!r}).")

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        # Position and length only. Never the content — see the class docstring.
        raise GroqNoteParseError(
            f"Groq returned malformed JSON for the note schema "
            f"(at char {exc.pos} of {len(content)}; finish_reason={choices[0].get('finish_reason')!r})."
        ) from exc

    if not isinstance(payload, dict):
        raise GroqNoteParseError(f"Groq returned a {type(payload).__name__}, not the expected note object.")
    return payload


class GroqNoteGenerator(NoteGenerator):
    """Groq-hosted note generation via the OpenAI-compatible chat
    completions endpoint, with strict schema-constrained decoding.

    The model is operator-configurable (`GROQ_NOTE_MODEL`) for the same
    reason `GroqWhisperProvider`'s is: Groq's catalogue moves, and models
    get deprecated on published dates. What is *not* freely configurable
    in practice is the model's release status — see the module docstring
    on why a preview model cannot carry PHI.
    """

    provider_name = "groq"

    def __init__(self) -> None:
        # A plain instance attribute rather than a `@property`, matching
        # GroqWhisperProvider: `NoteGenerator` has no model_version slot,
        # but keeping the resolved id on the instance makes it available
        # for logging and keeps the two providers shaped alike.
        self.model_version = get_settings().groq_note_model

    def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
        settings = get_settings()
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not set. Provision one before this can run against real audio.")

        if not transcript:
            # Nothing to generate from — and nothing worth spending a real
            # API call to be told that. Every section suppressed, honestly,
            # at zero cost.
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
            GROQ_CHAT_COMPLETIONS_ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.groq_note_model,
                "max_tokens": MAX_TOKENS,
                # Clinical documentation is the one place creative variation
                # is purely a liability: the same transcript should produce
                # the same note.
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": transcript_text},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": SCHEMA_NAME,
                        "strict": True,
                        "schema": note_schema(),
                    },
                },
            },
            # A single fused clinical note is a slow-but-real dependency,
            # not an unreachable one — contrast storage.py's short timeouts
            # for a startup check against a possibly nonexistent endpoint.
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
        )
        response.raise_for_status()

        sections = build_sections(_parse_payload(response.json()), valid_segment_ids)

        return GeneratedNote(
            assessment=sections["assessment"],
            plan=sections["plan"],
            subjective=sections["subjective"],
            objective=sections["objective"],
            provider=self.provider_name,
            prompt_version=PROMPT_VERSION,
        )


__all__ = ["GroqNoteGenerator", "GroqNoteParseError", "PROMPT_VERSION", "SECTION_NAMES"]
