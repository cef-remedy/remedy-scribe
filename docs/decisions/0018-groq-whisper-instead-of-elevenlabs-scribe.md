# 0018 — ASR vendor: Groq-hosted Whisper large-v3, not the PRD's named ElevenLabs Scribe v2

**Phase:** 1.3 · **Decided by:** user · **Date:** 2026-08-25

**Decision:** `get_asr_provider()` returns `GroqWhisperProvider`, calling
Groq's OpenAI-compatible `/audio/transcriptions` endpoint with
`whisper-large-v3`. `ElevenLabsScribeProvider` is deleted, not kept
alongside as a dormant alternative.

**This is a deviation from a written requirement, not an implementation
detail.** `remedy-scribe-prd.md`'s P0-3 explicitly says: "Integrates
ElevenLabs Scribe v2 (see model-selection proposal) **with speaker
diarization enabled**." Groq's hosted Whisper has no diarization
capability at all — Whisper is a transcription model; it has never had a
notion of "who is speaking," on Groq or any other host. This is not a
quality/tuning question the roadmap's "no bake-off" risk-acceptance
covers — it's a structural capability the chosen model doesn't have.

**What this costs, concretely:**
- Every segment/word gets a single placeholder speaker
  (`UNKNOWN_SPEAKER = "speaker_unknown"`, deliberately not styled like
  Scribe's real `speaker_0`/`speaker_1` labels, so nothing downstream
  mistakes "no diarization happened" for "diarization succeeded with one
  speaker").
- 1.3's own heads-up about mapping `speaker_0`/`speaker_1` to
  doctor/patient is now moot — there's nothing to map. That problem is
  either solved with a separate diarization step (not built here) or
  pushed entirely into Phase 1.4's note generation, inferring Subjective
  vs. Objective content from conversational context alone rather than a
  labeled speaker turn. This is a materially harder problem for the LLM
  to get right, and P0-4's correctness depends on it.
- Per-word confidence is also weaker than Scribe would have provided:
  Whisper's `verbose_json` exposes segment-level `avg_logprob`, not a
  genuine per-word score — `_confidence_from_avg_logprob` is a documented
  approximation applied uniformly across a segment's words, not a real
  per-word measurement.

**Why this is recorded as the user's call, not absorbed silently:** cost
and latency are the obvious reasons to prefer Groq (fast, cheap inference
vs. a per-vendor BAA/DPA negotiation that was already an open blocker for
Scribe per the roadmap) — but trading those for diarization is a product
decision with a direct line to P0-4's Subjective/Objective accuracy, not
a swap two equivalent options.

**What would change my mind / restore diarization:** if internal alpha
shows the note generator can't reliably separate doctor from patient
content without speaker labels (very plausible), the options in order of
increasing cost are: (a) a separate lightweight diarization pass (e.g.
pyannote.audio) run alongside Groq's transcription, timestamp-merged
after the fact; (b) a different hosted ASR vendor that diarizes *and*
meets the cost/latency bar Groq was chosen for; (c) accept the accuracy
hit and rely on prompt-level heuristics (the doctor speaks the consent
script first, etc.) applied to undiarized text — weakest option, listed
for completeness not as a recommendation.
