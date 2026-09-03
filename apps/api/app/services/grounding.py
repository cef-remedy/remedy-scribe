"""Grounding (Phase 3, P0-7) — resolving a note's `source_spans` back to
the transcript passages and audio timestamps they cite.

This is the product's trust mechanism. The doctor's rational response to
"an AI wrote this" is "prove it," and this module is the proof. Everything
upstream exists to make it honest: segment IDs assigned at persist time
(1.2), the model citing those IDs rather than inventing character offsets
(1.4), and citations verified against the segments actually sent rather
than trusted (`note_generation/shared.py`'s `build_section`, shared by
every provider precisely so this guarantee cannot vary by vendor).

Four decisions here are deliberate, and each one is a place where the
obvious implementation would produce a *confidently wrong* answer — which
is worse than no answer at all for a feature whose entire job is proof:

1. **Spans are validated against the note's current text, not trusted.**
   `text_start`/`text_end` are offsets into the text *as generated*. A
   doctor's edit shifts every offset after it, so highlighting by stored
   offsets after an edit would highlight the wrong words. `spans_fit`
   below re-derives the sentence boundaries from the text itself and
   reports whether the offsets still delimit it. When they don't, the UI
   must not highlight at all.

2. **"Edited since generation" is reported separately.** A same-length
   rewrite leaves the offsets structurally valid while making the content
   the doctor's rather than the model's. Presenting a transcript passage
   as "the source of this line" would then be false. Derived from
   `NoteRevision` existing for that section — no new column, and no
   dependence on revision ordering (see decision 0027 on why timestamp
   ordering is not something to lean on).

3. **Audio availability is verified against storage, not read from the
   database.** The bucket's own lifecycle rule expires objects after
   `audio_retention_days` with nothing updating `Encounter`, so
   `audio_object_key` set and `audio_deleted_at` NULL does *not* mean the
   bytes exist. Trusting the DB is exactly how a doctor gets the dead
   play button the Phase 3 heads-up warns about.

4. **Only cited segments and their immediate neighbours are returned**,
   never the whole transcript. The transcript is arguably more sensitive
   than the note (verbatim, including what the doctor chose not to write
   down), so shipping all of it to a browser to render a highlight is
   more exposure than the feature needs. Neighbours come along, dimmed,
   because a passage read without its surroundings is easy to
   misinterpret.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.models.consent import ConsentEventType, ConsentLedgerEntry
from app.models.encounter import Encounter
from app.models.note import Note, NoteRevision
from app.models.transcript import Transcript
from app.services import storage
from app.services.asr.base import TranscriptSegment
from app.services.transcripts import _json_to_segments

logger = logging.getLogger(__name__)

SECTION_NAMES = ("assessment", "plan", "subjective", "objective")

#: How many uncited segments either side of a cited one to include for
#: context. One is enough to tell "the patient answered a question" from
#: "the patient volunteered this," and small enough not to leak the
#: consult wholesale.
CONTEXT_SEGMENTS = 1


class AudioState(str, Enum):
    """The degradation ladder the Phase 3 heads-up asks for: notes outlive
    audio, and the doctor must understand *which* state they are in rather
    than seeing a play button that does nothing.
    """

    AVAILABLE = "available"
    NEVER_RECORDED = "never_recorded"
    #: Deleted at the patient's request (P0-1 withdrawal). Legally and
    #: clinically different from retention expiry, and worth saying so:
    #: the recording is gone because someone asked, not because time passed.
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    #: Object storage did not answer. Deliberately not folded into
    #: EXPIRED: "we could not check" and "it is gone" warrant different
    #: words to a doctor, and guessing the harsher one is still a guess.
    UNREACHABLE = "unreachable"


class TranscriptState(str, Enum):
    """The same ladder as AudioState, and it needs the same rung for the
    same reason.

    Phase 4.4 added a retention job that deletes a withdrawn encounter's
    transcript, not just its audio — correctly, since the transcript is
    verbatim PHI derived from a recording the patient asked to have
    destroyed. But with only EXPIRED available, that deletion was
    described to the doctor as "the retention period elapsed", which is
    the wrong reason. Decision 0030's whole argument for a five-state
    audio ladder was that a withdrawal and a retention expiry are
    observably identical and mean entirely different things; a
    three-state transcript ladder quietly contradicted it.
    """

    AVAILABLE = "available"
    NEVER_TRANSCRIBED = "never_transcribed"
    #: Deleted at the patient's request (P0-1), by the same purge that
    #: removed the audio.
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


@dataclass(frozen=True)
class GroundedSegment:
    """One transcript passage, with the timing the audio player needs.

    Timestamps are re-derived from the segment's own persisted words
    rather than stored a second time on the span — the same reasoning as
    not storing a transcript's `full_text` alongside its segments
    (decision 0016): one source of truth, and no chance of the copy
    drifting from the original.
    """

    id: str
    index: int
    speaker: str
    text: str
    start_ms: int | None
    end_ms: int | None
    #: False for a neighbour included only for context.
    cited: bool


@dataclass(frozen=True)
class GroundedSpan:
    text_start: int
    text_end: int
    segment_ids: list[str]
    #: The slice of the note's own text this span covers, resolved here so
    #: the client never has to re-slice by offset and get it subtly wrong.
    text: str


@dataclass(frozen=True)
class GroundedSection:
    suppressed: bool
    spans: list[GroundedSpan]
    #: Do the stored offsets still delimit the current text? When False the
    #: client must not highlight — see this module's docstring, point 1.
    spans_fit: bool
    #: Has a doctor edited this section since generation? See point 2.
    edited_since_generation: bool


@dataclass(frozen=True)
class Grounding:
    note_id: str
    encounter_id: str
    audio_state: AudioState
    transcript_state: TranscriptState
    segments: list[GroundedSegment]
    sections: dict[str, GroundedSection]


def _segment_timing(segment: TranscriptSegment) -> tuple[int | None, int | None]:
    """A diarized turn with no words should not exist, but the persisted
    JSON permits one, and `words[0]` on an empty list is a 500 on a read
    endpoint. Returns (None, None) so the caller can disable playback for
    that passage instead of the whole screen failing.
    """
    if not segment.words:
        return None, None
    return segment.words[0].start_ms, segment.words[-1].end_ms


def spans_fit_text(text: str, spans: list[dict]) -> bool:
    """Whether these stored offsets still describe `text`.

    Generation builds a section by joining per-sentence strings with a
    single space and recording each sentence's offsets as it goes (see
    `note_generation/shared.py`'s `build_section`, which every provider
    uses for exactly this reason). That makes the invariant checkable without
    storing the sentences a second time: slicing the text by the spans and
    re-joining with a space must reproduce the text exactly.

    Any insertion, deletion, or reordering breaks it. A same-length
    in-sentence substitution does not — correctly, because the offsets
    genuinely do still delimit that sentence; whether its *content* is
    still the model's is what `edited_since_generation` answers.
    """
    if not spans:
        # No spans means either a suppressed section or nothing cited.
        # Either way there is nothing to fit, and calling that "fits" would
        # invite the UI to highlight a section it has no spans for.
        return False
    ordered = sorted(spans, key=lambda s: s["text_start"])
    if ordered[0]["text_start"] != 0 or ordered[-1]["text_end"] != len(text):
        return False
    if any(s["text_start"] < 0 or s["text_end"] > len(text) or s["text_start"] > s["text_end"] for s in ordered):
        return False
    return " ".join(text[s["text_start"] : s["text_end"]] for s in ordered) == text


def _section_edited(db: Session, note_id: str, section: str) -> bool:
    """Any revision at all means the doctor has been in this section.

    An EXISTS rather than fetching rows: this asks a yes/no question about
    PHI-bearing text, so there is no reason to decrypt any of it.
    """
    return bool(
        db.query(exists().where(NoteRevision.note_id == note_id).where(NoteRevision.section == section)).scalar()
    )


def _audio_state(db: Session, encounter: Encounter) -> AudioState:
    """Resolves what actually happened to the recording.

    The order matters: the *reason* audio is gone is more useful to a
    doctor than the bare fact, so a recorded withdrawal is reported as a
    withdrawal even though the observable end state (no object) is
    identical to retention expiry.
    """
    if encounter.audio_object_key is None:
        return AudioState.NEVER_RECORDED

    if encounter.audio_deleted_at is not None:
        return AudioState.WITHDRAWN if _was_withdrawn(db, encounter.id) else AudioState.EXPIRED

    # The database says the object is there. That is not evidence: the
    # bucket lifecycle rule (storage.ensure_bucket_configured) expires
    # objects on its own after audio_retention_days, and nothing writes
    # back to this row when it does. So ask storage.
    try:
        head = storage.head_object(encounter.audio_object_key)
    except Exception:  # noqa: BLE001 - any storage failure is "we don't know"
        logger.warning("Could not check audio object for encounter %s", encounter.id, exc_info=True)
        return AudioState.UNREACHABLE

    if head is not None:
        return AudioState.AVAILABLE

    # Gone, and the row still claimed otherwise. Stamp it so the next read
    # is answered from the database and every later caller agrees — the
    # lifecycle rule is the only thing that could have removed it, so
    # retention expiry is a statement of fact here, not an inference.
    logger.info(
        "Audio object for encounter %s is gone; recording retention expiry",
        encounter.id,
    )
    encounter.audio_deleted_at = _now()
    db.add(encounter)
    db.commit()
    return AudioState.EXPIRED


def _now():  # noqa: ANN202 - trivial indirection, kept for test monkeypatching
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _was_withdrawn(db: Session, encounter_id: str) -> bool:
    return bool(
        db.query(
            exists()
            .where(ConsentLedgerEntry.encounter_id == encounter_id)
            .where(ConsentLedgerEntry.event == ConsentEventType.WITHDRAWN)
        ).scalar()
    )


def _select_segments(all_segments: list[TranscriptSegment], cited_ids: set[str]) -> list[GroundedSegment]:
    """Cited segments plus `CONTEXT_SEGMENTS` either side, in transcript
    order, each flagged with whether it was actually cited.
    """
    by_index = {i: seg for i, seg in enumerate(all_segments)}
    keep: set[int] = set()
    for i, seg in by_index.items():
        if seg.id in cited_ids:
            for j in range(i - CONTEXT_SEGMENTS, i + CONTEXT_SEGMENTS + 1):
                if j in by_index:
                    keep.add(j)

    out: list[GroundedSegment] = []
    for i in sorted(keep):
        seg = by_index[i]
        start_ms, end_ms = _segment_timing(seg)
        out.append(
            GroundedSegment(
                id=seg.id or f"seg{i}",
                index=i,
                speaker=seg.speaker,
                text=seg.text,
                start_ms=start_ms,
                end_ms=end_ms,
                cited=seg.id in cited_ids,
            )
        )
    return out


def resolve_grounding(db: Session, note: Note) -> Grounding:
    """Everything the grounding UI needs for one note, in one read.

    Deliberately does *not* mint a presigned audio URL. A URL is a live
    handle on PHI with a lifetime; minting one on every note open would
    hand out a playable link to a recording the doctor may never ask to
    hear. `presign_playback_url` does that on demand instead.
    """
    encounter = db.get(Encounter, note.encounter_id)
    audio_state = _audio_state(db, encounter) if encounter is not None else AudioState.NEVER_RECORDED

    raw_spans: dict[str, dict] = {}
    try:
        raw_spans = json.loads(note.source_spans or "{}")
    except json.JSONDecodeError:
        # A malformed blob means grounding is unavailable for this note, not
        # that the note cannot be read. Sections fall through to "no spans".
        logger.warning("Note %s has unparseable source_spans", note.id)

    transcript_row = db.query(Transcript).filter(Transcript.encounter_id == note.encounter_id).one_or_none()
    if transcript_row is None:
        # A note exists, so generation ran, so a transcript existed once —
        # its absence now is a deletion, not "never transcribed". The
        # distinction matters: one is a permanent loss of the source, the
        # other is a pipeline that has not finished.
        #
        # Which *kind* of deletion is read from the consent ledger, exactly
        # as the audio state is: the retention purge removes a withdrawn
        # encounter's transcript too, and reporting that as retention
        # expiry would tell the doctor the wrong reason.
        transcript_state = (
            TranscriptState.WITHDRAWN if _was_withdrawn(db, note.encounter_id) else TranscriptState.EXPIRED
        )
        all_segments: list[TranscriptSegment] = []
    else:
        transcript_state = TranscriptState.AVAILABLE
        all_segments = _json_to_segments(transcript_row.segments)

    sections: dict[str, GroundedSection] = {}
    cited_ids: set[str] = set()
    for name in SECTION_NAMES:
        entry = raw_spans.get(name) or {}
        stored = [s for s in entry.get("spans", []) if isinstance(s, dict)]
        text = getattr(note, name)
        fits = spans_fit_text(text, stored)
        spans = [
            GroundedSpan(
                text_start=s["text_start"],
                text_end=s["text_end"],
                segment_ids=list(s.get("segment_ids") or []),
                # Only meaningful if the offsets still fit; sliced anyway so
                # a client that ignores spans_fit gets something inspectable
                # rather than an empty string that looks like real data.
                text=text[s["text_start"] : s["text_end"]] if fits else "",
            )
            for s in sorted(stored, key=lambda s: s["text_start"])
        ]
        if fits:
            for span in spans:
                cited_ids.update(span.segment_ids)
        sections[name] = GroundedSection(
            suppressed=bool(entry.get("suppressed")),
            spans=spans,
            spans_fit=fits,
            edited_since_generation=_section_edited(db, note.id, name),
        )

    return Grounding(
        note_id=note.id,
        encounter_id=note.encounter_id,
        audio_state=audio_state,
        transcript_state=transcript_state,
        segments=_select_segments(all_segments, cited_ids),
        sections=sections,
    )


class AudioNotPlayableError(Exception):
    """Raised instead of returning a URL that would 404 or leak. Carries the
    doctor-facing reason, because "no audio" without a reason is the dead
    play button this phase exists to avoid.
    """

    def __init__(self, state: AudioState, message: str) -> None:
        super().__init__(message)
        self.state = state


_UNPLAYABLE_REASONS = {
    AudioState.NEVER_RECORDED: "No recording was ever uploaded for this consultation.",
    AudioState.WITHDRAWN: (
        "The recording was deleted at the patient's request. The transcript and note remain part of the record."
    ),
    AudioState.EXPIRED: (
        "The recording's retention period has elapsed and the audio has been deleted. "
        "The transcript and note remain part of the record."
    ),
    AudioState.UNREACHABLE: "Audio storage could not be reached just now. This is not a deletion — try again shortly.",
}


def presign_playback_url(db: Session, encounter: Encounter) -> tuple[str, int]:
    """A short-lived GET URL for the encounter's audio, minted on demand.

    Returns (url, expires_in_seconds).

    Two things make this "without permanently re-downloading PHI" (P0-7's
    own wording) rather than just "a download link":

    * **Range requests go straight to object storage.** A presigned GET
      supports them natively, so a browser `<audio>` element asking for
      one thirty-second window fetches only that window's bytes — and the
      API server never sees the audio at all, the same property the
      presigned *upload* path has (decision 0013).
    * **The URL tells the browser not to keep it.** `ResponseCacheControl`
      is signed into the URL, so storage returns `no-store` and the bytes
      are not written to the browser's HTTP cache. Nothing about playback
      persists PHI to the laptop's disk.

    ⚠️ **Both of those properties are S3's, not the app's.** On the Google
    Drive backend (decision 0040) there is no presigned GET and no way to
    set a response header, so `storage.presign_audio_playback` raises
    `UnsupportedByBackendError` and the route falls back to streaming the
    bytes through the API. That is a real loss, not a detail: PHI crosses
    the application server on the way out, and `no-store` becomes something
    the route sets by hand rather than something storage guarantees.
    """
    state = _audio_state(db, encounter)
    if state is not AudioState.AVAILABLE:
        raise AudioNotPlayableError(state, _UNPLAYABLE_REASONS[state])
    assert encounter.audio_object_key is not None  # implied by AVAILABLE
    return storage.presign_audio_playback(encounter.audio_object_key)


def playable_audio(db: Session, encounter: Encounter, range_header: str | None):
    """Audio bytes for a backend that cannot presign.

    Runs the *same* `_audio_state` check first, so a withdrawn or expired
    recording is refused with the same reason on either backend — the
    degradation ladder is a property of the app, not of the storage vendor.
    """
    state = _audio_state(db, encounter)
    if state is not AudioState.AVAILABLE:
        raise AudioNotPlayableError(state, _UNPLAYABLE_REASONS[state])
    assert encounter.audio_object_key is not None  # implied by AVAILABLE
    return storage.stream_object_range(encounter.audio_object_key, range_header)
