"""Phase 1.4: real note generation (Claude Haiku 4.5 — the sole provider,
decision 0021). No live API key is available here, same as every other
external integration in this codebase before its real key existed — so
the pure logic (prompt formatting, response parsing, span computation,
citation verification) carries the real test weight, plus one test that
builds a real, un-mocked httpx request (the lesson from Phase 1.3's
Groq multipart bug: a test that replaces httpx.post entirely proves the
call site looks right, never that httpx accepts what's sent).
"""

import httpx
import pytest

from app.services.asr.base import TranscriptSegment, TranscriptWord
from app.services.note_generation.haiku import (
    PROMPT_VERSION,
    TOOL_NAME,
    HaikuNoteGenerator,
    _build_section,
    _build_tool_schema,
    _extract_tool_input,
    _format_transcript,
)


def _word(text: str, confidence: float = 0.95, speaker: str = "speaker_unknown") -> TranscriptWord:
    return TranscriptWord(text=text, start_ms=0, end_ms=100, confidence=confidence, speaker=speaker)


# --- _format_transcript: mechanical suppression at the input, not a request ---


def test_low_confidence_words_become_inaudible_markers():
    segment = TranscriptSegment(
        id="seg0",
        speaker="speaker_unknown",
        words=[_word("Ano", confidence=0.9), _word("mumble", confidence=0.1), _word("po", confidence=0.9)],
    )

    rendered = _format_transcript([segment], low_confidence_threshold=0.5)

    assert "[INAUDIBLE]" in rendered
    assert "mumble" not in rendered
    assert "Ano" in rendered and "po" in rendered


def test_adjacent_inaudible_markers_are_collapsed():
    segment = TranscriptSegment(
        id="seg0",
        speaker="speaker_unknown",
        words=[_word("a", confidence=0.9), _word("b", confidence=0.1), _word("c", confidence=0.1), _word("d", confidence=0.9)],
    )

    rendered = _format_transcript([segment], low_confidence_threshold=0.5)

    assert rendered.count("[INAUDIBLE]") == 1


def test_segment_id_and_speaker_are_included_in_the_rendered_line():
    segment = TranscriptSegment(id="seg7", speaker="speaker_unknown", words=[_word("hi")])

    rendered = _format_transcript([segment], low_confidence_threshold=0.5)

    assert rendered.startswith("[seg7 | speaker_unknown]")


def test_multiple_segments_render_one_line_each_in_order():
    segments = [
        TranscriptSegment(id="seg0", speaker="speaker_unknown", words=[_word("first")]),
        TranscriptSegment(id="seg1", speaker="speaker_unknown", words=[_word("second")]),
    ]

    rendered = _format_transcript(segments, low_confidence_threshold=0.5)
    lines = rendered.split("\n")

    assert len(lines) == 2
    assert "first" in lines[0] and "second" in lines[1]


# --- tool schema shape -------------------------------------------------


def test_tool_schema_requires_all_four_sections():
    schema = _build_tool_schema()

    assert schema["name"] == TOOL_NAME
    assert set(schema["input_schema"]["required"]) == {"assessment", "plan", "subjective", "objective"}


# --- _extract_tool_input: pulling structured output out of the API response ---


def test_extract_tool_input_finds_the_named_tool_use_block():
    response = {
        "content": [
            {"type": "text", "text": "some preamble"},
            {"type": "tool_use", "name": TOOL_NAME, "input": {"assessment": {"suppressed": True, "sentences": []}}},
        ]
    }

    result = _extract_tool_input(response)

    assert result == {"assessment": {"suppressed": True, "sentences": []}}


def test_extract_tool_input_raises_if_the_tool_was_not_called():
    with pytest.raises(RuntimeError, match=TOOL_NAME):
        _extract_tool_input({"content": [{"type": "text", "text": "no tool call here"}]})


# --- _build_section: span computation and citation verification -------


def test_build_section_computes_offsets_by_concatenation():
    raw = {
        "suppressed": False,
        "sentences": [
            {"text": "Patient reports headache.", "segment_ids": ["seg0"]},
            {"text": "No fever noted.", "segment_ids": ["seg1"]},
        ],
    }

    section = _build_section(raw, valid_segment_ids={"seg0", "seg1"})

    assert section.text == "Patient reports headache. No fever noted."
    assert section.text[section.spans[0].text_start : section.spans[0].text_end] == "Patient reports headache."
    assert section.text[section.spans[1].text_start : section.spans[1].text_end] == "No fever noted."
    assert section.spans[0].segment_ids == ["seg0"]
    assert section.spans[1].segment_ids == ["seg1"]


def test_build_section_drops_hallucinated_citations_not_the_sentence():
    """The model citing an ID that was never in the transcript is
    treated as a mechanical error to correct, not trusted evidence —
    but the sentence itself (which may still be a real transcription
    of something) is kept, just uncited.
    """
    raw = {"suppressed": False, "sentences": [{"text": "Patient is well.", "segment_ids": ["seg0", "seg99"]}]}

    section = _build_section(raw, valid_segment_ids={"seg0"})

    assert section.text == "Patient is well."
    assert section.spans[0].segment_ids == ["seg0"]  # seg99 silently dropped, not trusted


def test_build_section_suppressed_forces_empty_text_even_with_sentences():
    """Mechanical enforcement, not trust: even if the model
    inconsistently supplied sentences alongside suppressed=true, the
    server-side rule is suppressed wins.
    """
    raw = {"suppressed": True, "sentences": [{"text": "should not appear", "segment_ids": []}]}

    section = _build_section(raw, valid_segment_ids=set())

    assert section.suppressed is True
    assert section.text == ""
    assert section.spans == []


def test_build_section_skips_empty_sentence_text():
    raw = {"suppressed": False, "sentences": [{"text": "", "segment_ids": []}, {"text": "real", "segment_ids": []}]}

    section = _build_section(raw, valid_segment_ids=set())

    assert section.text == "real"
    assert len(section.spans) == 1


# --- generate(): orchestration, API-key gate, cost-free empty-transcript path ---


def test_generate_raises_without_api_key(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        HaikuNoteGenerator().generate([TranscriptSegment(id="seg0", speaker="speaker_unknown", words=[_word("hi")])])

    get_settings.cache_clear()


def test_generate_short_circuits_on_empty_transcript_without_calling_the_api(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    called = {"n": 0}
    monkeypatch.setattr(
        "app.services.note_generation.haiku.httpx.post",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )

    note = HaikuNoteGenerator().generate([])

    assert called["n"] == 0  # no transcript, no reason to spend a real call
    assert note.assessment.suppressed and note.plan.suppressed
    assert note.subjective.suppressed and note.objective.suppressed
    assert note.prompt_version == PROMPT_VERSION

    get_settings.cache_clear()


def test_generate_end_to_end_with_a_mocked_response(monkeypatch):
    """The "golden transcript" case the checklist asks for: a fixed,
    realistic transcript in, fixed assertions on the resulting note out
    — including that a hallucinated citation gets scrubbed and a
    suppressed section comes back genuinely empty.
    """
    from app.core.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    transcript = [
        TranscriptSegment(
            id="seg0", speaker="speaker_unknown", words=[_word("Ano"), _word("po"), _word("ang"), _word("masakit")]
        ),
        TranscriptSegment(id="seg1", speaker="speaker_unknown", words=[_word("Ulo"), _word("ko")]),
    ]

    fake_response_body = {
        "content": [
            {
                "type": "tool_use",
                "name": TOOL_NAME,
                "input": {
                    "assessment": {"suppressed": True, "sentences": []},
                    "plan": {"suppressed": True, "sentences": []},
                    "subjective": {
                        "suppressed": False,
                        "sentences": [
                            {"text": "Patient reports headache.", "segment_ids": ["seg1", "seg-does-not-exist"]}
                        ],
                    },
                    "objective": {"suppressed": True, "sentences": []},
                },
            }
        ]
    }

    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return httpx.Response(200, json=fake_response_body, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.services.note_generation.haiku.httpx.post", _fake_post)

    note = HaikuNoteGenerator().generate(transcript)

    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["json"]["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert note.assessment.suppressed and note.assessment.text == ""
    assert note.subjective.text == "Patient reports headache."
    assert note.subjective.spans[0].segment_ids == ["seg1"]  # the fake ID never survives
    assert note.provider == "haiku"
    assert note.prompt_version == PROMPT_VERSION

    get_settings.cache_clear()


def test_generate_raises_on_http_error(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()

    def _fake_post(url, **kwargs):
        return httpx.Response(429, json={"error": "rate limited"}, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.services.note_generation.haiku.httpx.post", _fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        # Uncaught on purpose — tasks/pipeline.py's existing
        # self.retry(...) wrapper is what's supposed to catch this, not
        # this generator swallowing it.
        HaikuNoteGenerator().generate([TranscriptSegment(id="seg0", speaker="speaker_unknown", words=[_word("hi")])])

    get_settings.cache_clear()


# --- a real, un-mocked httpx request — the Phase 1.3 lesson applied here ---


def test_the_request_body_actually_encodes_as_valid_json():
    """Unlike Groq's multipart call, this one uses plain `json=`, which
    httpx handles uniformly — but this is still worth a real (no
    network) request-building check rather than only ever trusting a
    fully mocked `httpx.post`, per the exact gap that bit
    GroqWhisperProvider.transcribe in Phase 1.3.
    """
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    request = client.build_request(
        "POST",
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": "k", "anthropic-version": "2023-06-01"},
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 2048,
            "system": "sys",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [_build_tool_schema()],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        },
    )
    body = request.read()

    import json as _json

    decoded = _json.loads(body)
    assert decoded["tool_choice"]["name"] == TOOL_NAME
    assert decoded["messages"][0]["content"] == "hello"
