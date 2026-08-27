"""Grounding UI response shapes (Phase 3, P0-7)."""

from pydantic import BaseModel

from app.services.grounding import AudioState, TranscriptState


class GroundedSegmentOut(BaseModel):
    """One transcript passage. `start_ms`/`end_ms` are nullable because a
    persisted segment with no words is representable, and a read endpoint
    should degrade to "this passage cannot be played" rather than 500.
    """

    id: str
    index: int
    speaker: str
    text: str
    start_ms: int | None
    end_ms: int | None
    #: False for a neighbour included only as context — the client dims
    #: these rather than presenting them as evidence for the note line.
    cited: bool

    model_config = {"from_attributes": True}


class GroundedSpanOut(BaseModel):
    text_start: int
    text_end: int
    segment_ids: list[str]
    text: str

    model_config = {"from_attributes": True}


class GroundedSectionOut(BaseModel):
    suppressed: bool
    spans: list[GroundedSpanOut]
    #: When false, the stored offsets no longer delimit the section's
    #: current text and the client **must not** highlight by them.
    spans_fit: bool
    edited_since_generation: bool

    model_config = {"from_attributes": True}


class GroundingOut(BaseModel):
    note_id: str
    encounter_id: str
    #: The degradation ladder: audio + transcript, transcript only, or
    #: neither. The client says which one it is in words, because "the
    #: doctor should understand which state they're in, not just see a
    #: dead play button."
    audio_state: AudioState
    transcript_state: TranscriptState
    segments: list[GroundedSegmentOut]
    sections: dict[str, GroundedSectionOut]

    model_config = {"from_attributes": True}


class AudioPlaybackOut(BaseModel):
    """A short-lived presigned GET URL. Minted only when the doctor asks to
    hear something, never as part of loading a note.
    """

    url: str
    expires_in_seconds: int
