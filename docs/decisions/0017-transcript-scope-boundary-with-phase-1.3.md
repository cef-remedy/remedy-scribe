# 0017 — What belongs on Transcript now vs. what Phase 1.3 owns

**Phase:** 1.2 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** `Transcript` gets `asr_provider` (a plain name, e.g.
`"elevenlabs_scribe_v2"`) now. It does **not** get an `asr_model_version`
column yet, and `retention_expires_at` is computed using the same
`audio_retention_days` policy as the audio, set automatically on every
persist.

**Options considered — provider tracking:** (a) add `asr_provider` now,
leave `asr_model_version` for 1.3, as chosen; (b) add both now with
`asr_model_version` nullable/unset; (c) add neither, since "record which
ASR provider and model version" is explicitly a Phase 1.3 checklist item.

**Why:** (c) would leave the new table with no notion of provenance at
all — awkward for a table whose whole job is to hold something durable,
and every other generated-content table in this schema
(`Note.note_generator_provider`) already tracks its own provider. (b)
would add a column with no real value to put in it: there's no live ASR
API call yet (`transcribe()` still raises `NotImplementedError`), so any
"model version" recorded today would be a placeholder pretending to be
real data — exactly the kind of claim this checklist keeps warning
against. (a) gives the table a minimally truthful sense of provenance
now, using a value (`ASRProvider.provider_name`, new on the base class)
that's true today, and leaves the *actually* 1.3-scoped column for when
1.3 has something non-fake to put in it.

**Why `retention_expires_at` is not the same kind of anticipation as
0011 warned against:** 0011 declined to add enum members for *unknown
future values* (Phase 1.5's not-yet-designed failure states). This
column applies an *already-decided, already-configured* policy
(`audio_retention_days`) to a new row that obviously needs it under that
same policy — not a guess about what might exist later.

**What would change my mind:** nothing about this now; it resolves
automatically when Phase 1.3 wires the real ElevenLabs call and has an
actual model-version string to record.
