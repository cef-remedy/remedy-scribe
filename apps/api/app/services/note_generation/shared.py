"""The parts of note generation that must be **identical across every
provider**, extracted when a second provider arrived (Groq, decision 0035).

This module exists for a correctness reason, not a DRY one. Two things
below are load-bearing for features implemented in entirely different
phases, and a provider that reimplemented either of them slightly
differently would break those features *silently*:

1. **`build_section`'s span convention.** It joins sentences with a
   single space and records each sentence's character offsets as it goes.
   Phase 3's grounding UI re-derives exactly that invariant to decide
   whether a note's stored offsets still fit its text
   (`app/services/grounding.py:spans_fit_text` slices by the spans and
   re-joins with a single space, expecting the original back). If one
   provider joined with a newline, every note it generated would report
   "source links no longer line up" from the moment it was created — a
   feature failing quietly, on one vendor only, with nothing raising.

2. **Citation verification.** Cited segment IDs are checked against the
   segments actually sent and dropped when they don't match. A
   hallucinated citation is worse than an uncited sentence, because it
   looks like evidence. This must not be re-decided per provider.

`_format_transcript`'s `[INAUDIBLE]` substitution is here for the same
reason: P0-4's suppression requirement is enforced *mechanically*, by
never showing the model the unreliable words, rather than by asking it
nicely. That guarantee is only as good as its least careful provider.
"""

from __future__ import annotations

from typing import Any

from app.services.asr.base import TranscriptSegment
from app.services.note_generation.base import GeneratedSection, SourceSpan

SECTION_NAMES = ("assessment", "plan", "subjective", "objective")

#: The shared instruction body. Each provider prepends its own sentence
#: about *how* to return the structure (a forced tool call for Anthropic,
#: a schema-constrained JSON object for Groq), because that part is a
#: genuine API difference rather than a difference of clinical intent.
SYSTEM_PROMPT_BODY = """The transcript is given to you as a series of lines, \
each tagged with a stable ID like [seg3 | speaker_unknown], in \
chronological order. Some words are replaced with the literal marker \
[INAUDIBLE] — that word's transcription was too unreliable to trust; do \
not guess what it might have been, and do not paraphrase around it as if \
you know what was said.

Produce exactly four sections, in this order: Assessment, Plan, \
Subjective, Objective. For each section:
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


def format_transcript(transcript: list[TranscriptSegment], low_confidence_threshold: float) -> str:
    """One line per segment: `[seg_id | speaker] word word [INAUDIBLE] ...`.

    Adjacent [INAUDIBLE] markers are collapsed to one — a run of
    unreliable words is one gap, not one marker per word lost.
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


def section_schema() -> dict[str, Any]:
    """One section's shape.

    `additionalProperties: false` and a fully-populated `required` list
    are not decoration: Groq's (OpenAI-compatible) **strict** structured
    output mode rejects a schema without them. Anthropic's tool
    `input_schema` does not require either, but accepts both — so one
    schema serves both providers, and the note's shape stays a single
    definition rather than two that can drift.
    """
    return {
        "type": "object",
        "additionalProperties": False,
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
                    "additionalProperties": False,
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


def note_schema() -> dict[str, Any]:
    """The whole note: four sections, all required."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {name: section_schema() for name in SECTION_NAMES},
        "required": list(SECTION_NAMES),
    }


def build_section(raw: dict[str, Any], valid_segment_ids: set[str]) -> GeneratedSection:
    """Turn one section of a provider's parsed output into a
    `GeneratedSection`, assigning character offsets server-side.

    ⚠️ The join convention here is a contract with Phase 3, not a local
    formatting choice — see this module's docstring. Change the separator
    and every note's grounding silently stops lining up.
    """
    if raw.get("suppressed"):
        # Suppression wins over content even if the model inconsistently
        # also supplied sentences — mechanical enforcement, not trust.
        return GeneratedSection(text="", spans=[], suppressed=True)

    parts: list[str] = []
    spans: list[SourceSpan] = []
    cursor = 0
    for sentence in raw.get("sentences") or []:
        text = sentence.get("text") or ""
        if not text:
            continue

        # Verified, not trusted: a cited ID that isn't one of the segments
        # we actually sent is dropped, not kept as if it were real
        # evidence. This is the check that lets the grounding UI present a
        # citation as proof at all.
        cited_ids = [sid for sid in sentence.get("segment_ids") or [] if sid in valid_segment_ids]

        if parts:
            cursor += 1  # the separator " ".join(parts) will insert
        start = cursor
        end = start + len(text)
        spans.append(SourceSpan(text_start=start, text_end=end, segment_ids=cited_ids))
        parts.append(text)
        cursor = end

    return GeneratedSection(text=" ".join(parts), spans=spans, suppressed=False)


def build_sections(payload: dict[str, Any], valid_segment_ids: set[str]) -> dict[str, GeneratedSection]:
    """Every section, defaulting a missing one to an empty (non-suppressed)
    section rather than raising.

    A provider that omitted a key has misbehaved, but the honest rendering
    of "the model said nothing about this section" is an empty section the
    doctor can fill in — not a failed pipeline run that loses the three
    sections it *did* produce.
    """
    return {name: build_section(payload.get(name) or {}, valid_segment_ids) for name in SECTION_NAMES}
