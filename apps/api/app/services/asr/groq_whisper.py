from __future__ import annotations

import math
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.asr.base import ASRProvider, TranscriptSegment, TranscriptWord
from app.services.storage import download_object

GROQ_TRANSCRIPTIONS_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"

# Phase 1.3: chosen over ElevenLabs Scribe v2 (the PRD's named vendor —
# see docs/decisions/0018 for why, and what this gives up). Whisper is a
# transcription model, not a diarization one — there is no per-speaker
# label anywhere in its output, on Groq or anywhere else it's hosted.
# Every word/segment gets this single placeholder rather than a fabricated
# "speaker_0"/"speaker_1" that would misleadingly imply successful
# diarization. Deliberately not the same shape as Scribe's real
# `speaker_0` labels, so nothing downstream can mistake this for "one
# speaker successfully identified out of several."
UNKNOWN_SPEAKER = "speaker_unknown"


class GroqWhisperProvider(ASRProvider):
    """Groq-hosted Whisper large-v3, called through Groq's OpenAI-
    compatible `/audio/transcriptions` endpoint.

    ⚠️ No diarization. This is the load-bearing fact about this
    provider: stock Whisper has no concept of "who is speaking" — its
    output is a plain, chronologically-ordered transcript. Every segment
    and word this returns is labeled `UNKNOWN_SPEAKER`. The checklist's
    "map speaker_0/speaker_1 to doctor/patient" heuristics (1.3's own
    heads-up) don't apply here because there's no speaker_0/speaker_1 to
    map — that problem is now either solved with a separate diarization
    step (not built here) or pushed downstream into note generation
    inferring roles from content alone. See docs/decisions/0018.
    """

    provider_name = "groq_whisper_large_v3"

    @property
    def model_version(self) -> str:
        # A property, not a fixed class attribute: GROQ_WHISPER_MODEL is
        # operator-configurable (e.g. swapping to a turbo variant), so
        # what gets recorded per-transcript should reflect the setting
        # actually used for that call, not a hardcoded string.
        return get_settings().groq_whisper_model

    def transcribe(self, audio_object_key: str) -> list[TranscriptSegment]:
        settings = get_settings()
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not set. Provision one before this can run against real audio.")

        audio_bytes = download_object(audio_object_key)
        filename = audio_object_key.rsplit("/", 1)[-1]

        response = httpx.post(
            GROQ_TRANSCRIPTIONS_ENDPOINT,
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            files={"file": (filename, audio_bytes)},
            data=[
                ("model", settings.groq_whisper_model),
                ("response_format", "verbose_json"),
                # Repeated form field, not a single JSON-array value — this
                # is how OpenAI's (and Groq's compatible) API expects a
                # multi-valued field in multipart/form-data.
                ("timestamp_granularities[]", "segment"),
                ("timestamp_granularities[]", "word"),
            ],
            # Whisper transcription of a full consult can genuinely take
            # a while server-side; this is a slow-but-real dependency, not
            # an unreachable one (contrast storage.py's short timeouts for
            # a startup check against a possibly-nonexistent endpoint).
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
        )
        response.raise_for_status()
        return self._parse_response(response.json())

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> list[TranscriptSegment]:
        """Documented shape (OpenAI Whisper verbose_json, which Groq
        mirrors for API compatibility): a top-level `segments` array
        (each with `start`/`end`/`text`/`avg_logprob`, chronologically
        ordered — Whisper decodes audio sequentially, so this ordering
        is inherent, not something this function has to impose) and a
        separate, flat top-level `words` array (`word`/`start`/`end`,
        no segment association). This function's actual job is bucketing
        each word into the segment whose time range contains it — get
        that wrong and you reproduce the *shape* of the original Scribe
        bug (turn order scrambled) even though the failure mechanism
        would be different. Kept pure and unit-testable against a
        hand-built fixture, same reasoning as the old Scribe stub: no
        live API key needed to verify the parsing logic itself. Verify
        this against a real Groq response before trusting it in
        production — this is documented behavior, not observed.
        """
        raw_segments = payload.get("segments", [])
        raw_words = payload.get("words", [])

        segments: list[TranscriptSegment] = []
        word_index = 0
        for raw_segment in raw_segments:
            segment_end = raw_segment["end"]
            confidence = _confidence_from_avg_logprob(raw_segment.get("avg_logprob"))

            words: list[TranscriptWord] = []
            # raw_words is flat and chronological; consume from where the
            # previous segment left off rather than rescanning from the
            # start — preserves order by construction and is O(n) overall.
            while word_index < len(raw_words) and raw_words[word_index]["start"] < segment_end:
                w = raw_words[word_index]
                words.append(
                    TranscriptWord(
                        text=w["word"],
                        start_ms=int(w["start"] * 1000),
                        end_ms=int(w["end"] * 1000),
                        confidence=confidence,
                        speaker=UNKNOWN_SPEAKER,
                    )
                )
                word_index += 1

            if words:  # a segment with no matched words contributes nothing
                segments.append(TranscriptSegment(speaker=UNKNOWN_SPEAKER, words=words))

        # Any trailing words past the last segment's boundary (clock-skew
        # / rounding at the very end of the recording) still belong in the
        # transcript — drop them and you silently lose the tail of the
        # consult, which is a worse failure than one slightly-off segment.
        if word_index < len(raw_words):
            trailing = raw_words[word_index:]
            segments.append(
                TranscriptSegment(
                    speaker=UNKNOWN_SPEAKER,
                    words=[
                        TranscriptWord(
                            text=w["word"],
                            start_ms=int(w["start"] * 1000),
                            end_ms=int(w["end"] * 1000),
                            confidence=_confidence_from_avg_logprob(None),
                            speaker=UNKNOWN_SPEAKER,
                        )
                        for w in trailing
                    ],
                )
            )

        return segments


def _confidence_from_avg_logprob(avg_logprob: float | None) -> float:
    """Whisper's verbose_json exposes `avg_logprob` per *segment*, not a
    genuine per-word confidence score — Whisper doesn't emit one. This
    is a documented approximation (exp of a log-probability is not
    literally P(word correct)), applied uniformly to every word in a
    segment, not a real measurement. Default to a neutral 0.5, not a
    confident-looking 1.0, when the field is missing — P0-4's
    silence/low-confidence suppression logic (Phase 1.4) reads this
    value, so overstating it here would defeat that safeguard rather
    than just being cosmetically wrong.
    """
    if avg_logprob is None:
        return 0.5
    return max(0.0, min(1.0, math.exp(avg_logprob)))
