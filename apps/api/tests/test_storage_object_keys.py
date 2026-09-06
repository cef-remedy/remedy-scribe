"""build_audio_object_key — pure string logic, no backend needed, so
these run without a container unlike test_storage_specific.py.

Found live: a real browser recording sat at pipeline_status="recording"
forever, then failed transcription with Groq returning a plain 400. The
actual cause was two bugs stacked in this one function:

1. MediaRecorder's real mimeType is "audio/webm;codecs=opus" — with
   parameters, not the bare "audio/webm" this dict's lookup was keyed on.
   The lookup missed every real recording and silently fell back to the
   generic ".audio" extension.
2. Even the bare "audio/webm" -> ".weba" mapping was itself wrong: tested
   directly against Groq's transcription endpoint with the actual failed
   audio bytes, ".weba" 400s ("unsupported_audio_format") and ".webm"
   200s with a real transcript. Groq identifies audio format from the
   filename extension, and "weba" was never on its accepted list (flac,
   mp3, mp4, mpeg, mpga, m4a, ogg, opus, wav, webm).

This used to be duplicated "identically, deliberately" in storage_s3.py
and storage_drive.py (that duplication is exactly how it could have been
half-fixed and left broken for one backend) -- now lives once, in the
dispatcher, since the logic has no S3/Drive-specific behavior at all.
"""

from app.services.storage import build_audio_object_key


def test_a_bare_content_type_maps_to_its_real_extension():
    key = build_audio_object_key("enc-1", "audio/wav")
    assert key.startswith("encounters/enc-1/audio/")
    assert key.endswith(".wav")


def test_the_real_browser_mime_type_with_codec_params_resolves_correctly():
    """The actual value MediaRecorder produces (session.ts's pickMimeType) —
    not a hypothetical edge case."""
    key = build_audio_object_key("enc-1", "audio/webm;codecs=opus")
    assert key.endswith(".webm")


def test_webm_never_maps_to_weba_again():
    """The regression this file exists for: confirmed against Groq's real
    API that .weba 400s and .webm doesn't (both against the same audio
    bytes) -- see this file's own docstring."""
    key = build_audio_object_key("enc-1", "audio/webm")
    assert not key.endswith(".weba")
    assert key.endswith(".webm")


def test_an_unrecognized_content_type_falls_back_to_a_generic_extension():
    key = build_audio_object_key("enc-1", "application/x-mystery-codec")
    assert key.endswith(".audio")


def test_a_missing_content_type_falls_back_to_a_generic_extension():
    key = build_audio_object_key("enc-1", None)
    assert key.endswith(".audio")


def test_two_calls_for_the_same_encounter_produce_different_keys():
    """Each recording gets its own object -- a resumed/re-recorded session
    must not silently overwrite an earlier upload under the same key."""
    first = build_audio_object_key("enc-1", "audio/wav")
    second = build_audio_object_key("enc-1", "audio/wav")
    assert first != second
