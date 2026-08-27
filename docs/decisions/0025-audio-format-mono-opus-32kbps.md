# 0025 — Audio capture format: mono Opus at 32 kbps

**Phase:** 2.1 (decided) / 2.2 (consumed) · **Decided by:** user · **Date:** 2026-08-27

**Decision:** capture speech as **mono Opus at 32 kbps**. Constants live in
`apps/web/src/lib/audio-config.ts`; Phase 2.2's recorder consumes them.

This resolves checklist 2.2's 🧠 *"audio format and bitrate"* call, brought
forward from 2.2 to 2.1 because the capture harness produced a concrete
number worth reacting to before any recorder was written.

## What prompted it

The first 29-minute harness run on real clinic hardware recorded **129 kbps
stereo — 26.7 MB** (`docs/experiments/runs/capture-harness-Windows-Chrome-20900943.json`).
It did that despite the harness requesting `channelCount: 1`: the Intel
Smart Sound device simply reported back `channelCount: 2`, and
`MediaRecorder` was given no explicit bitrate so it chose its own.

At ~20 consults a clinic day that is roughly **550 MB/day** uploading over
clinic wifi. Mono Opus at 32 kbps is ~240 KB/min, so the same day is about
**115 MB** — a bit under 5× smaller.

## Why 32 kbps mono is the right point on the curve

- **Opus is exceptionally good at speech at low bitrates.** 32 kbps mono is
  comfortably transparent for conversational voice; the format was designed
  for exactly this. The ASR model never hears anything a human listener
  would notice missing.
- **Whisper resamples to 16 kHz mono internally regardless.** Feeding it a
  48 kHz stereo stream discards most of that data inside the model anyway,
  so the extra bytes buy nothing measurable in accuracy — they only cost
  upload time on the constraint the PRD actually names (clinic wifi) and
  per-consult storage.
- **The second channel is near-duplicate.** One room microphone with two
  channels captures the same acoustic scene twice. It would matter for a
  stereo pair used for spatial separation — which could in principle help
  the diarization gap from decision 0018 — but the laptop's built-in array
  does not expose usable spatial channels, so there is nothing to exploit.
- **It stays well clear of the retention/cost story.** ~7 MB per 30-minute
  consult keeps IndexedDB queueing trivial and keeps the PRD's
  `<$0.10/consult` target unaffected by transfer.

## What this decision does *not* assume

**That asking will work.** The harness run is direct evidence that a
`channelCount: 1` constraint can be silently ignored. So the implementation
does three things rather than one:

1. Requests the constraint (`AUDIO_CONSTRAINTS`).
2. Sets `audioBitsPerSecond` explicitly on `MediaRecorder` — without it the
   browser picks, which is how 129 kbps happened.
3. Calls `assertAudioSettings(track)` after acquisition and reports any
   mismatch between requested and actual. It returns the mismatches rather
   than throwing: a stereo track is worth logging loudly and downmixing,
   not worth refusing to record a consultation over.

Point 3 is the general lesson this project keeps re-learning — a requested
setting is not an achieved setting, and the difference has to be measured
rather than assumed (see also decision 0010's enum constraints and 0014's
MinIO lifecycle rules).

**That one container fits every browser.** `pickMimeType()` feature-detects
in preference order rather than hardcoding: Chrome/Edge/Firefox give
WebM/Opus, Safari was MP4/AAC-only before 18.4. Groq Whisper accepts both,
so the only wrong move is assuming one. Note the consequence: on an
older Safari the "Opus" half of this decision is unavailable and capture
falls back to AAC — acceptable, but it means the 32 kbps target should be
treated as Opus-specific and re-checked if AAC ever becomes the primary path.

**That echo cancellation and noise suppression help.** Both are left off.
They are tuned for intelligible conference calls, not faithful
transcription, and aggressive noise suppression can clip the onset of quiet
speech — which for Taglish code-switching is exactly where ASR accuracy is
already weakest (decision 0019).

## What would change my mind

- If alpha shows measurable transcription errors that track audio quality
  rather than the model, 48–64 kbps mono is the next step and still ~2×
  smaller than the harness's accidental 129 kbps stereo. Change the
  constant, not the code.
- If a real diarization approach arrives that needs genuine multi-channel
  input (decision 0018's open options), stereo stops being duplicate data
  and this trade reopens — but that requires a mic array that actually
  exposes spatial channels, which the current laptop does not.
- If Safari/AAC becomes a primary target rather than a fallback, re-derive
  the bitrate: AAC needs more bits than Opus for equivalent speech quality.
