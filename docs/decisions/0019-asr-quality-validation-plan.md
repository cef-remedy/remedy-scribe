# 0019 — Validating ASR quality with no vendor bake-off: two-person test audio, checked for speaker separation

**Phase:** 1.3 · **Decided by:** user · **Date:** 2026-08-25

**Decision:** before internal alpha, gather informal two-person
conversational audio (not necessarily medical) and run it through
`GroqWhisperProvider` to see whether the output shows any usable
speaker-separation signal.

**Set the expectation correctly before running this test:** stock
Whisper's output contains no speaker information whatsoever — not
"unreliable diarization," but *zero* diarization mechanism. Every
segment this provider returns is labeled `UNKNOWN_SPEAKER` by
construction (see `app/services/asr/groq_whisper.py`). Running two-person
audio through it will not produce a different result than one-person
audio in terms of speaker separation — there is no code path in Whisper
or in this provider that could differentiate them. This test is genuinely
useful for confirming that fact concretely (finding it by running it,
not just by reading a doc), and for sanity-checking transcription
*accuracy* on Taglish/code-switched speech (which Whisper does attempt,
unlike diarization) — but it will not answer "can this differentiate
speakers" with anything other than "no," because the model doesn't
attempt that at all.

**What this test is actually good for, reframed:** transcription
accuracy on informal, code-switched, multi-person audio — word error
rate by ear, whether Filipino/English code-switching mid-sentence
garbles output, whether Groq's `avg_logprob` values (this provider's
confidence proxy) actually track perceived transcription quality. All
useful, cheap, and exactly the kind of "pick something and start
measuring" the roadmap's bake-off risk-acceptance calls for — just not a
diarization test, because there's no diarization to test.

**What would change my mind:** nothing about the *expectation* — this
is a documented model capability, not a hypothesis. What this test
result *should* drive is decision 0018's "what would change my mind":
if accuracy on real Taglish audio looks bad even before diarization
enters the picture, that's a reason to reconsider Groq Whisper itself,
independent of the diarization gap.
