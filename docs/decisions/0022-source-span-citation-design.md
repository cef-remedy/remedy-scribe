# 0022 — Source spans cite segment IDs; the model never emits offsets or timestamps

**Phase:** 1.4 · **Decided by:** implementation (the checklist's own 🧠 already named the winning option; this record is about what fell out of applying it) · **Date:** 2026-08-25

**Decision:** `SourceSpan` cites `segment_ids: list[str]` — the transcript
segment `id` already assigned at persistence time (decision 0016), not a
new sentence-numbering scheme built for this phase. The model is handed
each transcript segment with its stable ID in the prompt (`[seg3 |
speaker_unknown] ...`) and cites those IDs when it emits a sentence; it
is never asked for a character offset, a millisecond timestamp, or
anything it would have to count or measure. `text_start`/`text_end` are
offsets into the *note's own generated text*, computed server-side by
tracking a cursor while concatenating the model's per-sentence output —
mathematically exact, because the server controls the concatenation.

**Why not the checklist's other two options:**
- *Quote-and-search* (model quotes the exact passage verbatim, server
  string-searches the transcript for it) works but is strictly more
  expensive for no real gain here: it burns tokens re-emitting text the
  model already has a cheap, exact handle for (the segment ID), and a
  quote can still fail to match verbatim (punctuation, casing, an
  [INAUDIBLE] marker sitting inside the quoted span) in ways an ID
  lookup cannot.
- *Raw offsets/timestamps from the model* is the option the checklist
  itself predicted would fail — LLMs asked to count characters produce
  confident, wrong numbers — and this project already has an unrelated,
  concrete example of exactly that failure mode (Phase 1.3's turn-order
  bug came from trusting structure the model wasn't actually tracking
  correctly).

**Citations are verified, not trusted.** Any `segment_id` the model
cites that doesn't match one of the segment IDs actually sent in that
prompt is dropped in `_build_section` before the note is ever saved —
silently, not by rejecting the whole sentence. A hallucinated citation
is worse than an uncited sentence, because it looks like evidence when
it isn't; an uncited sentence is honestly what it looks like.

**Suppression is enforced the same way — mechanically, twice.** Low-
confidence words are replaced with a literal `[INAUDIBLE]` in the prompt
text before the model ever sees them (`_format_transcript`), and the
tool schema forces the model to set a `suppressed` boolean per section
explicitly; `_build_section` forces `text=""` whenever `suppressed` is
true regardless of whatever sentences the model also emitted. Neither
layer depends on the model choosing to comply with an instruction.

**Resolving a segment ID to a real audio timestamp** (for the grounding
UI's eventual "play from here," P0-7, Phase 3) happens at *read* time —
look the ID up against the transcript's own persisted segment, get its
words' timings — not by storing timestamps redundantly on `SourceSpan`.
Same reasoning decision 0016 already used for not storing a transcript's
`full_text` separately from its segments: derive it from data that's
already there rather than duplicating it.

**What would change my mind:** if the grounding UI (Phase 3) turns out
to need sub-segment precision — pointing at one word inside a long
segment, not the whole turn — `segment_ids` alone won't carry that, and
citations would need to additionally reference a word index or a time
range inside the segment. Nothing here forecloses that; it's additive to
the same JSON shape, the same way decision 0016 left room for a future
`sentence_id`.
