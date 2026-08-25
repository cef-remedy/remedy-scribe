"""Transcript persistence (Phase 1.2) — the missing half of two places
that already claimed to handle this: `transcribe_encounter` computed
`segments` and discarded them (`_ = segments`), and `generate_note`
always called the model with `transcript=[]`. This module is the one
place that converts between `ASRProvider`'s dataclasses
(app/services/asr/base.py) and `Transcript.segments`' JSON shape, so
neither the Celery task nor the note generator has to know that shape
exists.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.transcript import Transcript
from app.services.asr.base import TranscriptSegment, TranscriptWord


def _segments_to_json(segments: list[TranscriptSegment]) -> list[dict]:
    # The explicit "id" override must come *after* the ** spread: as of
    # Phase 1.4, TranscriptSegment itself carries an `id` field (usually
    # None pre-persistence), and dataclasses.asdict() would include that
    # None — placed first, the spread would silently overwrite the real
    # "seg{i}" id with None. Dict literals resolve duplicate keys to the
    # last one written, so ordering here isn't cosmetic.
    return [{**dataclasses.asdict(segment), "id": f"seg{i}"} for i, segment in enumerate(segments)]


def _json_to_segments(data: list[dict]) -> list[TranscriptSegment]:
    return [
        TranscriptSegment(id=seg["id"], speaker=seg["speaker"], words=[TranscriptWord(**w) for w in seg["words"]])
        for seg in data
    ]


def persist_transcript(
    db: Session,
    encounter_id: str,
    *,
    provider_name: str,
    model_version: str | None = None,
    segments: list[TranscriptSegment],
) -> Transcript:
    """Upserts on `encounter_id`'s unique constraint — idempotent on its
    own, in addition to `transcribe_encounter`'s existing no-op check
    for an already-transcribed encounter (belt and suspenders: a
    redelivered Celery message that somehow got past that check still
    overwrites cleanly here rather than violating the unique constraint).
    """
    existing = db.query(Transcript).filter(Transcript.encounter_id == encounter_id).one_or_none()
    retention_expires_at = datetime.now(timezone.utc) + timedelta(days=get_settings().audio_retention_days)

    if existing is not None:
        existing.asr_provider = provider_name
        existing.asr_model_version = model_version
        existing.segments = _segments_to_json(segments)
        existing.retention_expires_at = retention_expires_at
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing

    row = Transcript(
        encounter_id=encounter_id,
        asr_provider=provider_name,
        asr_model_version=model_version,
        segments=_segments_to_json(segments),
        retention_expires_at=retention_expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def load_transcript(db: Session, encounter_id: str) -> list[TranscriptSegment]:
    """Returns `[]` when nothing has been persisted yet — matches
    `generate_note`'s pre-1.2 default, so this is a strict improvement
    with no new failure mode for callers that haven't checked. A caller
    that needs to distinguish "not yet transcribed" from "transcribed
    but genuinely silent" should query for the `Transcript` row itself
    instead of relying on this.
    """
    row = db.query(Transcript).filter(Transcript.encounter_id == encounter_id).one_or_none()
    if row is None:
        return []
    return _json_to_segments(row.segments)
