# 0011 — `EncounterPipelineStatus` membership: exactly the 5 values in use today

**Phase:** 0.4 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** the new `EncounterPipelineStatus` enum has exactly the 5
members already written somewhere in the codebase (`recording`, `uploaded`,
`transcribed`, `note_generated`, `blocked_no_consent`) — no speculative
members added for failure states Phase 1.5 hasn't built yet.

**Options considered:** (a) exactly today's 5 values, as chosen; (b) also
add placeholder members for anticipated Phase 1.5 failure states
(`upload_failed`, `transcription_failed`, `generation_failed`, ...) now,
since a migration is being written anyway.

**Why:** (b) means guessing at Phase 1.5's actual design (the checklist
asks for "a specific, actionable error state per failure mode" — plural,
and possibly more granular than one member per pipeline stage) before that
phase has been thought through, and an enum member added speculatively now
is exactly the kind of unused-but-plausible-looking code this checklist
elsewhere warns against (the `require_role` dependency that was "defined
but never attached" in 0.2). Extending this enum later is cheap — add a
member, add one migration line — so there's no cost to waiting.

**What would change my mind:** nothing changes this now; revisit
automatically when Phase 1.5 is implemented, at which point its actual
failure taxonomy should drive the new members, not a guess made here.
