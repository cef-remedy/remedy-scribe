"""Phase 1.3: Groq-hosted Whisper large-v3 (docs/decisions/0018 — chosen
over the PRD's named ElevenLabs Scribe v2). No live API key is available
here, same as the ASR/note-generation stubs before this phase — so
`_parse_response` (a pure function) carries the real test weight, built
against a hand-crafted fixture matching Groq's documented verbose_json
shape. The one thing worth re-testing every time this class changes:
words land in the right segment, in the right order — that's the exact
shape of bug the old ElevenLabs `_parse_response` had (grouped by
speaker across the whole recording, destroying turn order).
"""

import httpx
import pytest

from app.services.asr.groq_whisper import (
    UNKNOWN_SPEAKER,
    GroqWhisperProvider,
    _confidence_from_avg_logprob,
)


def _groq_response(segments, words):
    return {"task": "transcribe", "language": "english", "duration": 10.0, "text": "...", "segments": segments, "words": words}


# --- _parse_response: the actual risk in this code ------------------------


def test_words_are_bucketed_into_the_correct_segment_in_order():
    payload = _groq_response(
        segments=[
            {"id": 0, "start": 0.0, "end": 1.0, "text": " Ano po ang", "avg_logprob": -0.1},
            {"id": 1, "start": 1.5, "end": 2.5, "text": " Masakit ulo", "avg_logprob": -0.2},
        ],
        words=[
            {"word": "Ano", "start": 0.0, "end": 0.3},
            {"word": "po", "start": 0.3, "end": 0.5},
            {"word": "ang", "start": 0.5, "end": 0.9},
            {"word": "Masakit", "start": 1.6, "end": 2.0},
            {"word": "ulo", "start": 2.0, "end": 2.4},
        ],
    )

    segments = GroqWhisperProvider._parse_response(payload)

    assert len(segments) == 2
    assert [w.text for w in segments[0].words] == ["Ano", "po", "ang"]
    assert [w.text for w in segments[1].words] == ["Masakit", "ulo"]
    # Turn order preserved by construction, not by chance — segments and
    # words are both consumed strictly in the order Groq returned them.
    assert segments[0].words[0].start_ms < segments[1].words[0].start_ms


def test_every_word_and_segment_gets_the_unknown_speaker_placeholder():
    payload = _groq_response(
        segments=[{"id": 0, "start": 0.0, "end": 1.0, "text": " hi", "avg_logprob": -0.1}],
        words=[{"word": "hi", "start": 0.0, "end": 0.5}],
    )

    segments = GroqWhisperProvider._parse_response(payload)

    assert segments[0].speaker == UNKNOWN_SPEAKER
    assert segments[0].words[0].speaker == UNKNOWN_SPEAKER


def test_trailing_words_past_the_last_segment_are_not_dropped():
    """Clock-skew / rounding at the tail of a recording shouldn't mean
    losing the last few words spoken — silently truncating the end of a
    consult is a worse failure than one imperfectly-bounded segment.
    """
    payload = _groq_response(
        segments=[{"id": 0, "start": 0.0, "end": 1.0, "text": " hi there", "avg_logprob": -0.1}],
        words=[
            {"word": "hi", "start": 0.0, "end": 0.4},
            {"word": "there", "start": 0.4, "end": 0.9},
            {"word": "friend", "start": 1.3, "end": 1.8},  # past segment 0's end=1.0, no segment 1 exists
        ],
    )

    segments = GroqWhisperProvider._parse_response(payload)

    all_words = [w.text for seg in segments for w in seg.words]
    assert all_words == ["hi", "there", "friend"]


def test_a_segment_with_no_matching_words_contributes_nothing():
    payload = _groq_response(
        segments=[
            {"id": 0, "start": 0.0, "end": 1.0, "text": " silence", "avg_logprob": -3.0},
            {"id": 1, "start": 1.0, "end": 2.0, "text": " hi", "avg_logprob": -0.1},
        ],
        words=[{"word": "hi", "start": 1.1, "end": 1.5}],
    )

    segments = GroqWhisperProvider._parse_response(payload)

    assert len(segments) == 1
    assert segments[0].words[0].text == "hi"


def test_empty_response_yields_no_segments():
    assert GroqWhisperProvider._parse_response(_groq_response(segments=[], words=[])) == []


# --- confidence approximation ----------------------------------------------


def test_confidence_defaults_to_neutral_when_avg_logprob_missing():
    assert _confidence_from_avg_logprob(None) == 0.5


def test_confidence_is_clamped_to_zero_one():
    assert 0.0 <= _confidence_from_avg_logprob(-10.0) <= 1.0
    assert 0.0 <= _confidence_from_avg_logprob(0.0) <= 1.0


def test_confidence_decreases_as_avg_logprob_worsens():
    assert _confidence_from_avg_logprob(-0.1) > _confidence_from_avg_logprob(-2.0)


# --- provenance: what gets recorded on the persisted Transcript -----------


def test_model_version_reflects_the_configured_setting(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
    get_settings.cache_clear()

    assert GroqWhisperProvider().model_version == "whisper-large-v3-turbo"

    get_settings.cache_clear()


# --- transcribe(): the API-key gate -----------------------------------------


def test_transcribe_raises_without_api_key(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("GROQ_API_KEY", "")
    get_settings.cache_clear()

    provider = GroqWhisperProvider()
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        provider.transcribe("encounters/x/audio/y.m4a")

    get_settings.cache_clear()


def test_transcribe_downloads_audio_and_posts_to_groq(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    get_settings.cache_clear()

    monkeypatch.setattr(
        "app.services.asr.groq_whisper.download_object", lambda key: b"fake-audio-bytes"
    )

    captured = {}

    def _fake_post(url, headers=None, files=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["files"] = files
        captured["data"] = data
        return httpx.Response(
            200,
            json={
                "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": " hi", "avg_logprob": -0.1}],
                "words": [{"word": "hi", "start": 0.0, "end": 0.5}],
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.services.asr.groq_whisper.httpx.post", _fake_post)

    provider = GroqWhisperProvider()
    segments = provider.transcribe("encounters/enc-1/audio/y.m4a")

    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["files"]["file"][0] == "y.m4a"
    assert ("model", "whisper-large-v3") in captured["data"]
    assert len(segments) == 1
    assert segments[0].words[0].text == "hi"

    get_settings.cache_clear()


def test_transcribe_raises_on_http_error(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    get_settings.cache_clear()
    monkeypatch.setattr("app.services.asr.groq_whisper.download_object", lambda key: b"fake-audio-bytes")

    def _fake_post(url, **kwargs):
        return httpx.Response(429, json={"error": "rate limited"}, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.services.asr.groq_whisper.httpx.post", _fake_post)

    provider = GroqWhisperProvider()
    with pytest.raises(httpx.HTTPStatusError):
        # Uncaught here on purpose — tasks/pipeline.py's existing
        # self.retry(...) wrapper is what's supposed to catch this
        # (checklist: "handle rate limits ... with Celery retries
        # (already scaffolded)"), not this provider swallowing it.
        provider.transcribe("encounters/enc-1/audio/y.m4a")

    get_settings.cache_clear()
