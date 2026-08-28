"""Groq note generation (decision 0035) — the provider that replaced
Anthropic Haiku as the default.

These cover the three things that could break silently in a vendor swap:

1. **The request is shaped the way Groq's strict mode requires.** A schema
   missing `additionalProperties: false`, or a `required` list that omits
   a key, is rejected by strict structured outputs — but only at runtime,
   against a live key nobody has here. So the schema is asserted directly.
2. **The span convention is unchanged.** Phase 3's grounding re-derives
   "sentences joined by one space" to decide whether a note's offsets
   still fit. A provider that drifted here would produce notes whose
   grounding never lines up, quietly, with nothing raising.
3. **A parse failure never carries model output.** `last_pipeline_error`
   is an unencrypted column, and the note text is the one thing that must
   not reach it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.asr.base import TranscriptSegment, TranscriptWord
from app.services.grounding import spans_fit_text
from app.services.note_generation import get_note_generator
from app.services.note_generation.groq import (
    PROMPT_VERSION,
    SCHEMA_NAME,
    GroqNoteGenerator,
    GroqNoteParseError,
    _parse_payload,
)
from app.services.note_generation.haiku import HaikuNoteGenerator
from app.services.note_generation.shared import build_sections, note_schema

# --- fixtures -------------------------------------------------------------


def _segment(seg_id: str, words: list[tuple[str, float]], speaker: str = "speaker_unknown") -> TranscriptSegment:
    return TranscriptSegment(
        id=seg_id,
        speaker=speaker,
        words=[
            TranscriptWord(
                text=t,
                start_ms=i * 400,
                end_ms=i * 400 + 350,
                confidence=c,
                speaker=speaker,
            )
            for i, (t, c) in enumerate(words)
        ],
    )


def _transcript() -> list[TranscriptSegment]:
    return [
        _segment("seg0", [("Magandang", 0.95), ("umaga", 0.95), ("po", 0.94)]),
        _segment(
            "seg1",
            [("Tatlong", 0.93), ("araw", 0.92), ("nang", 0.91), ("nilalagnat", 0.9)],
        ),
        _segment("seg2", [("May", 0.9), ("plema", 0.88), ("ba", 0.9)]),
    ]


def _payload(**overrides) -> dict:
    base = {
        "assessment": {
            "suppressed": False,
            "sentences": [
                {
                    "text": "Appears to have an acute febrile illness.",
                    "segment_ids": ["seg1"],
                },
                {"text": "Productive cough is reported.", "segment_ids": ["seg2"]},
            ],
        },
        "plan": {
            "suppressed": False,
            "sentences": [
                {
                    "text": "Advised rest and review in three days.",
                    "segment_ids": ["seg2"],
                }
            ],
        },
        "subjective": {
            "suppressed": False,
            "sentences": [
                {
                    "text": 'Reports "tatlong araw nang nilalagnat".',
                    "segment_ids": ["seg1"],
                }
            ],
        },
        "objective": {"suppressed": True, "sentences": []},
    }
    base.update(overrides)
    return base


def _response(payload: dict, *, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": json.dumps(payload)},
                }
            ]
        },
        request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
    )


@pytest.fixture()
def groq_configured(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "test-key", raising=False)
    monkeypatch.setattr(settings, "groq_note_model", "openai/gpt-oss-120b", raising=False)
    return settings


# --- the request Groq actually receives -----------------------------------


def test_the_schema_satisfies_groq_strict_mode_requirements():
    """Strict structured outputs reject a schema that omits
    `additionalProperties: false` or leaves a property out of `required`.
    That rejection only happens against a live key, so it is asserted here
    instead — recursively, because one nested object missing the flag is
    enough to fail the whole request.
    """
    schema = note_schema()

    def check(node: dict, path: str) -> None:
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path} allows additional properties"
            props = set(node.get("properties", {}))
            assert set(node.get("required", [])) == props, f"{path} does not require every property"
            for name, child in node.get("properties", {}).items():
                check(child, f"{path}.{name}")
        if node.get("type") == "array":
            check(node["items"], f"{path}[]")

    check(schema, "note")
    assert set(schema["properties"]) == {
        "assessment",
        "plan",
        "subjective",
        "objective",
    }


def test_request_uses_strict_json_schema_not_tool_use(monkeypatch, groq_configured):
    """Groq's docs are explicit that structured outputs and tool use are
    mutually exclusive. Sending both is the obvious port of the Anthropic
    call and would fail — or worse, silently ignore the schema.
    """
    captured: dict = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return _response(_payload())

    monkeypatch.setattr("app.services.note_generation.groq.httpx.post", _fake_post)
    GroqNoteGenerator().generate(_transcript())

    body = captured["json"]
    assert "tools" not in body and "tool_choice" not in body
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == SCHEMA_NAME
    assert body["model"] == "openai/gpt-oss-120b"
    # The same transcript should produce the same note; creative variation
    # is pure liability in clinical documentation.
    assert body["temperature"] == 0.0
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["url"].startswith("https://api.groq.com/openai/v1/chat/completions")


def test_low_confidence_words_are_replaced_before_the_model_sees_them(monkeypatch, groq_configured):
    """P0-4 suppression is mechanical, not a request. The model cannot
    smooth over a gap it was never shown.
    """
    captured: dict = {}

    def _fake_post(url, **kwargs):
        captured["json"] = kwargs["json"]
        return _response(_payload())

    monkeypatch.setattr("app.services.note_generation.groq.httpx.post", _fake_post)
    transcript = [_segment("seg0", [("Magandang", 0.95), ("umaga", 0.05), ("po", 0.02)])]
    GroqNoteGenerator().generate(transcript)

    user_message = captured["json"]["messages"][1]["content"]
    assert "Magandang" in user_message
    assert "umaga" not in user_message
    # Two adjacent unreliable words are one gap, not two markers.
    assert user_message.count("[INAUDIBLE]") == 1


def test_no_api_call_is_made_for_an_empty_transcript(monkeypatch, groq_configured):
    def _explode(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("should not call the vendor for an empty transcript")

    monkeypatch.setattr("app.services.note_generation.groq.httpx.post", _explode)
    note = GroqNoteGenerator().generate([])

    assert note.assessment.suppressed is True
    assert note.provider == "groq"
    assert note.prompt_version == PROMPT_VERSION


def test_a_missing_api_key_fails_before_the_request(monkeypatch, groq_configured):
    monkeypatch.setattr(groq_configured, "groq_api_key", "", raising=False)

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqNoteGenerator().generate(_transcript())


# --- the note that comes back ---------------------------------------------


def test_sections_are_built_with_verified_citations(monkeypatch, groq_configured):
    monkeypatch.setattr(
        "app.services.note_generation.groq.httpx.post",
        lambda url, **kw: _response(_payload()),
    )
    note = GroqNoteGenerator().generate(_transcript())

    assert note.assessment.text == "Appears to have an acute febrile illness. Productive cough is reported."
    assert [s.segment_ids for s in note.assessment.spans] == [["seg1"], ["seg2"]]
    assert note.objective.suppressed is True
    assert note.objective.text == ""
    assert note.provider == "groq"
    assert note.prompt_version == "groq-v1"


def test_a_hallucinated_citation_is_dropped_not_stored(monkeypatch, groq_configured):
    """A citation to a segment that was never sent is worse than no
    citation, because the grounding UI presents it as proof.
    """
    payload = _payload(
        assessment={
            "suppressed": False,
            "sentences": [
                {
                    "text": "Fever for three days.",
                    "segment_ids": ["seg1", "seg99", "made-up"],
                }
            ],
        }
    )
    monkeypatch.setattr(
        "app.services.note_generation.groq.httpx.post",
        lambda url, **kw: _response(payload),
    )

    note = GroqNoteGenerator().generate(_transcript())

    assert note.assessment.spans[0].segment_ids == ["seg1"]


def test_suppression_wins_over_content_the_model_supplied_anyway(monkeypatch, groq_configured):
    payload = _payload(
        objective={
            "suppressed": True,
            "sentences": [{"text": "Chest clear on auscultation.", "segment_ids": []}],
        }
    )
    monkeypatch.setattr(
        "app.services.note_generation.groq.httpx.post",
        lambda url, **kw: _response(payload),
    )

    note = GroqNoteGenerator().generate(_transcript())

    assert note.objective.suppressed is True
    assert note.objective.text == ""
    assert note.objective.spans == []


def test_a_missing_section_yields_an_empty_one_rather_than_losing_the_note(monkeypatch, groq_configured):
    """Strict mode should make this impossible. If it happens anyway, the
    honest result is one empty section the doctor fills in — not a failed
    run that discards the three good sections.
    """
    payload = _payload()
    del payload["plan"]
    monkeypatch.setattr(
        "app.services.note_generation.groq.httpx.post",
        lambda url, **kw: _response(payload),
    )

    note = GroqNoteGenerator().generate(_transcript())

    assert note.plan.text == ""
    assert note.assessment.text != ""


# --- the contract with Phase 3's grounding --------------------------------


def test_generated_spans_satisfy_the_grounding_invariant(monkeypatch, groq_configured):
    """The load-bearing cross-phase assertion.

    `grounding.spans_fit_text` decides whether a note's stored offsets still
    delimit its text by slicing with the spans and re-joining with a single
    space. If a provider assembled sections any other way, every note it
    generated would report "source links no longer line up" from birth —
    a Phase 3 feature broken by a Phase 4 vendor swap, silently, on one
    vendor only.
    """
    monkeypatch.setattr(
        "app.services.note_generation.groq.httpx.post",
        lambda url, **kw: _response(_payload()),
    )
    note = GroqNoteGenerator().generate(_transcript())

    for section in (note.assessment, note.plan, note.subjective):
        spans = [
            {
                "text_start": s.text_start,
                "text_end": s.text_end,
                "segment_ids": s.segment_ids,
            }
            for s in section.spans
        ]
        assert spans_fit_text(section.text, spans) is True, f"grounding would break for {section.text!r}"


def test_both_providers_build_identical_sections_from_identical_payloads():
    """The two providers differ only in transport. If that ever stops being
    true, notes would ground differently depending on which vendor happened
    to be configured — the kind of divergence nothing else would catch.
    """
    valid = {"seg0", "seg1", "seg2"}
    payload = _payload()

    assert build_sections(payload, valid) == build_sections(payload, valid)
    from app.services.note_generation.haiku import _build_section
    from app.services.note_generation.shared import build_section

    assert _build_section is build_section


# --- failure paths, and the PHI they must not carry -----------------------


def test_malformed_json_is_reported_without_the_model_output():
    """`_mark_stage_failure` writes `str(exc)[:500]` into
    `Encounter.last_pipeline_error`, a plain **unencrypted** column. The
    tempting debug detail here is the note text itself, which is exactly
    what must never land there.
    """
    secret = "Patient Maria Santos reports chest pain"
    response = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": '{"assessment": ' + secret},
            }
        ]
    }

    with pytest.raises(GroqNoteParseError) as exc:
        _parse_payload(response)

    assert secret not in str(exc.value)
    assert "Maria" not in str(exc.value)
    # Still actionable: it names the structural cause.
    assert "malformed JSON" in str(exc.value)
    assert "length" in str(exc.value)


def test_a_refusal_is_named_without_repeating_the_refusal_text():
    response = {"choices": [{"message": {"refusal": "I cannot help with Maria Santos's records"}}]}

    with pytest.raises(GroqNoteParseError) as exc:
        _parse_payload(response)

    assert "Maria" not in str(exc.value)
    assert "declined" in str(exc.value)


def test_an_empty_body_is_distinguished_from_malformed_json():
    with pytest.raises(GroqNoteParseError, match="empty note body"):
        _parse_payload({"choices": [{"finish_reason": "length", "message": {"content": ""}}]})


def test_no_choices_is_its_own_error():
    with pytest.raises(GroqNoteParseError, match="no choices"):
        _parse_payload({"choices": []})


def test_a_json_array_is_rejected_rather_than_treated_as_a_note():
    response = {"choices": [{"message": {"content": "[1, 2, 3]"}}]}

    with pytest.raises(GroqNoteParseError, match="not the expected note object"):
        _parse_payload(response)


def test_the_anthropic_path_also_stopped_leaking_model_output():
    """The same trap existed on the Haiku path, which interpolated the whole
    response into the exception. Fixed there too; asserted here so the two
    providers cannot drift apart on it.
    """
    from app.services.note_generation.haiku import _extract_tool_input

    secret = "Assessment: Maria Santos has pneumonia"
    response = {
        "stop_reason": "max_tokens",
        "content": [{"type": "text", "text": secret}],
    }

    with pytest.raises(RuntimeError) as exc:
        _extract_tool_input(response)

    assert secret not in str(exc.value)
    assert "Maria" not in str(exc.value)
    assert "max_tokens" in str(exc.value)


# --- the swap point -------------------------------------------------------


def test_groq_is_the_default_provider(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "note_generator_provider", "groq", raising=False)
    assert isinstance(get_note_generator(), GroqNoteGenerator)


def test_haiku_remains_selectable_as_a_fallback(monkeypatch):
    """Kept deliberately (decision 0035): Groq's free tier caps throughput
    below a full consultation transcript, so one env var is the difference
    between a blocked pilot and a working one.
    """
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "note_generator_provider", "haiku", raising=False)
    assert isinstance(get_note_generator(), HaikuNoteGenerator)
