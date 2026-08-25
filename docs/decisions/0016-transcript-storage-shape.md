# 0016 — Transcript storage: encrypted JSON blob, segment-level (not sentence-level) granularity

**Phase:** 1.2 · **Decided by:** user (JSONB) + implementation (the rest) · **Date:** 2026-08-25

**Decision:** the transcript lives in a new `transcripts` table, one row
per encounter, with a `segments` column holding the whole diarized,
word-timed structure as one encrypted JSON blob (`EncryptedJSON`, a new
`TypeDecorator` alongside `EncryptedString`) — the user's chosen option
from Phase 1.2's three-way call. Two things followed that weren't fully
settled by "JSONB" alone:

1. **"JSONB" means shape here, not Postgres's native `JSONB` type.**
   The heads-up in the checklist requires the transcript get the same
   encryption as the audio. Once the payload is encrypted, the column
   is opaque ciphertext no matter what type it's declared as — native
   JSON path queries are off the table either way. So it's stored as
   plain `Text`, identically on Postgres and SQLite, the same reasoning
   `EncryptedString` already used for the same problem.
2. **The addressable unit is the ASR "segment," not a "sentence."** My
   own earlier framing (in conversation, paraphrasing the checklist)
   said "sentence as the addressable unit" — but nothing in the ASR
   pipeline produces sentence boundaries today. A "segment" is
   currently one speaker-diarized turn (and even that grouping has a
   known bug — Phase 1.3's `_parse_response` groups by speaker across
   the *whole* recording, not by turn). Inventing sentence-splitting in
   this phase would mean guessing at a decision (1.4's grounding-citation
   granularity: sentence IDs vs. quoted-span search vs. raw offsets)
   that's explicitly still open for a *later* phase.

**Why segment-level, not sentence-level, right now:** each segment
carries its own `words` list with individual timings, so nothing is
lost — a future sentence-splitter (in 1.3 or 1.4) can derive sentences
from `words`' timings without a migration, by re-grouping the same
stored data. Committing to sentence boundaries now, before 1.3 even
fixes the turn-grouping bug those boundaries would sit inside, would be
building on a foundation this project already knows is wrong.

**What would change my mind:** once Phase 1.4 actually decides its
citation granularity, if it needs sentence-stable IDs specifically (not
segment IDs), add a `sentence_id` alongside `id` inside each segment's
JSON at that point — additive, no migration needed since the column is
already JSON.
