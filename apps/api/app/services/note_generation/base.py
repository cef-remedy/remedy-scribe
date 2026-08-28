from __future__ import annotations

import dataclasses
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.services.asr.base import TranscriptSegment


@dataclass
class SourceSpan:
    """One reference from a generated note line back to the transcript
    passage it came from — P0-4 ("every generated line is traceable back
    to its source transcript passage") and P0-7 (grounding UI: tap a
    line, see the passage).

    `text_start`/`text_end` are character offsets into the *note's own
    section text* (`GeneratedSection.text`) — computed server-side by
    concatenating the model's per-sentence output (see haiku.py), never
    asked of the model directly. `segment_ids` point into the persisted
    `Transcript.segments`' stable `id` field (e.g. "seg0") — the model
    cites these IDs rather than transcript character offsets or raw
    timestamps, per the checklist's own reasoning: an LLM asked to emit
    offsets produces confident, wrong numbers, but citing a small
    identifier it was handed in the prompt is cheap and verifiable.

    Resolving a segment_id to an actual audio timestamp (for the
    grounding UI's "play from here") happens at *read* time by looking
    the id up in the transcript's own persisted words, not by storing
    timestamps redundantly here — the same reasoning as not storing a
    transcript's full_text separately from its segments (decision 0016).
    """

    text_start: int
    text_end: int
    segment_ids: list[str]


@dataclass
class GeneratedSection:
    text: str
    spans: list[SourceSpan] = field(default_factory=list)
    suppressed: bool = False  # True over silent/low-confidence audio windows (P0-4)


@dataclass
class GeneratedNote:
    assessment: GeneratedSection
    plan: GeneratedSection
    subjective: GeneratedSection
    objective: GeneratedSection
    provider: str  # "haiku" today (decision 0021 dropped "luna") — stored on Note.note_generator_provider
    prompt_version: str  # stored on Note.prompt_version — see haiku.py's PROMPT_VERSION

    def source_spans_json(self) -> str:
        """The JSON persisted to `Note.source_spans` — one object per
        section, each holding whether it was suppressed and its list of
        spans. Suppression lives here (not a separate DB column) because
        it's a property of *this generation*, not something anything
        else needs to query independently of the spans it explains.
        """
        sections = {
            "assessment": self.assessment,
            "plan": self.plan,
            "subjective": self.subjective,
            "objective": self.objective,
        }
        return json.dumps(
            {
                name: {
                    "suppressed": section.suppressed,
                    "spans": [dataclasses.asdict(s) for s in section.spans],
                }
                for name, section in sections.items()
            }
        )


class NoteGenerator(ABC):
    """P0-4 originally specified "Luna primary, Claude Haiku 4.5 configured
    fallback." As of the 2026-08-25 planning update (docs/decisions/0021),
    Haiku is the sole provider — Luna is dropped, not kept dormant. This
    interface is what still makes the provider a config flag
    (NOTE_GENERATOR_PROVIDER) rather than a rebuild, if a second real
    provider ever exists — see get_note_generator() in __init__.py.
    """

    @abstractmethod
    def generate(self, transcript: list[TranscriptSegment]) -> GeneratedNote:
        """Produce an Assessment/Plan/Subjective/Objective note.

        Implementations must suppress (not fabricate) content over
        low-confidence or silent windows, and must default to hedged
        clinical language rather than flat certainty — both are explicit
        P0-4 requirements, not implementation details left to the model.
        """
        raise NotImplementedError
