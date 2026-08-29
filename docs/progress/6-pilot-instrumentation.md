# Phase 6 — Pilot instrumentation

**Status:** done · **Date:** 2026-08-29
**Related decisions:** [0039](../decisions/0039-a-minor-edit-is-small-and-clinically-inert.md)
(what a "minor edit" is — the 🧠 deferred from 2.6),
[0027](../decisions/0027-consent-flow-ordering-and-withdrawal.md) (why
revision ordering is not something to lean on),
[0033](../decisions/0033-retention-is-enforced-in-two-layers.md) (why the
metric has to be frozen), [0035](../decisions/0035-groq-note-generation.md)
(the second unvalidated vendor this metric now carries)

Everything the PRD promised to measure now has code behind it, which is the
whole point of doing this before alpha rather than after.

## The heads-up has aged worse than it was written

> "The roadmap's stated mitigation for skipping the vendor bake-off is
> 'watch the edit-burden metric closely from day one of internal alpha.'
> That mitigation only exists if the metric is instrumented *before* alpha."

Since that was written, **decision 0035 swapped the note generator to a
different vendor entirely** — also without a bake-off, also on the strength
of watching this metric. The mitigation is now carrying two unvalidated
vendor choices, and until this phase it was carrying them with nothing
underneath.

## The definition, and why the obvious one is unsafe

An edit is minor when it is **small and clinically inert**. Both halves are
load-bearing, because a pure distance threshold produces this:

| edit | distance | what it actually is |
|---|---|---|
| `500mg` → `5000mg` | 1 char | a **10× overdose** |
| `500mg` → `500mcg` | 1 char | a **1000× error**, same digits |
| `no chest pain` → `chest pain` | 3 chars | an **inverted finding** |

Each is maximally minor by distance and among the most consequential
corrections a doctor can make. A metric scoring them as "the draft was
basically right" reports the model as most trustworthy exactly where it was
most dangerous — in the number being used to justify skipping vendor
validation.

So the clinical check is a **veto**, not a weighting: no similarity score
can make a changed dose minor. It does not claim to understand an edit; it
claims to recognise the categories it must not call minor — quantities, dose
units, negations, **including Filipino negations** (`wala`, `hindi`,
`walang`), because P0-3 keeps Taglish verbatim and a negation flip in the
patient's own words is the same inversion.

The error budget is deliberately asymmetric. A false positive makes the
metric pessimistic — a number that under-sells the product. A false negative
is a changed dose reported as trivial. Only one is recoverable.

## Three ways to get the arithmetic wrong

**Summing revisions instead of comparing endpoints.** Edits save on blur
(2.6), so typing a word, deleting it and retyping it makes three revisions
and zero net change. The target asks how far the *signed* note is from the
*drafted* one, so it compares two texts and uses revisions only to recover
the first.

**Ordering revisions by timestamp.** Decision 0027 already recorded that
identical `created_at` values make that non-deterministic. Instead the
generated text is found structurally: **the `previous_text` that never
appears as any `new_text`**. Edits chain A→B→C and only A is nobody's
output — ordering-independent, no new column. Its one genuine ambiguity, an
edit-then-revert, is *reported* rather than guessed at.

**Computing it later.** Phase 4.4's retention purge deletes revisions while
the signed note is permanent, so a metric depending on live revisions would
quietly become uncomputable mid-pilot. It is frozen at signing, keyed
`(note_id, definition_version)` so a definition change writes new rows
beside the old ones instead of rewriting history.

## What was built

- **`services/edit_burden.py`** — the definition, versioned.
- **`services/pilot_metrics.py`** — capture at signing, plus the six report
  surfaces.
- **`models/pilot.py`** — `NoteQualityMetric` (frozen) and `EncounterRating`.
  **Neither stores PHI** by design: ratios, booleans, durations, star counts,
  and safety flags naming *categories* ("numeric value changed") never text.
  That is what lets the report be read by whoever decides go/no-go without
  clinical access, and what keeps these rows outside the retention purge that
  must eventually delete the notes they describe.
- **`routes/pilot.py`** — rating submission, the report, and the weekly
  review sample.
- **`components/RatingPrompt.tsx`** — appears *after* signing, dismissible,
  one click to submit. It is the only pilot signal nothing can derive, so
  response rate is the thing to protect; a prompt that blocks trains people
  to click a star at random, which is worse than no data because it looks
  like data.

**Capture can never block a signature.** `capture_note_quality` swallows its
own errors — a deliberate inversion of this codebase's fail-loudly instinct.
Signing is the legal act that makes a doctor accountable (P0-5); a bug in a
similarity ratio must not refuse one. The compensating control is `coverage`
(measured ÷ signed) on the report, so silent loss is visible rather than
quietly biasing the headline.

## Two metrics reported with their limits attached

**"Correctly filed" is a caught-error rate.** The system sees *rejected*
filings (P0-6's 409s). It cannot see a note filed to the wrong patient that
the doctor confirmed anyway — at that point every check agrees. Reporting it
as the true rate would be the most flattering possible reading of the data;
the real figure needs the weekly review.

**Documentation time is split in two.** Encounter-to-signature compares to
the week-0 paper baseline; generation-to-signature is what the product
controls. A doctor who leaves a note open over lunch inflates the first and
not the second.

**The review sample is deterministic and flag-first.** The same week returns
the same ids, so two reviewers read the same notes and a reviewer can stop
and resume — `random.sample` would make disagreement uninterpretable. It
returns **ids only**: an endpoint returning note text would be an unaudited
bulk PHI export wearing a different hat.

## Verification

**419 API tests passing** (up from 385 — 34 new), `ruff` and `mypy` clean
across 69 source files, `tsc` clean, migration gate reports **no drift**.

| | |
|---|---|
| a changed dose is never minor, however small the edit | pass |
| a changed *unit* is caught with identical digits | pass |
| a flipped negation is never minor | pass |
| a **Filipino** negation flip is caught too | pass |
| typing-then-deleting scores as no change | pass |
| chain reconstruction survives identical timestamps | pass |
| edit-then-revert is reported ambiguous, not guessed | pass |
| one rewritten section disqualifies the whole note | pass |
| capture never blocks a signature | pass |
| capture is idempotent per definition version | pass |
| no data reports `None`, not `0.0` | pass |
| coverage exposes notes that were never measured | pass |
| usage counts distinct weeks, not just volume | pass |
| the rating comment is encrypted at rest | pass |
| the pilot report contains no PHI | pass |
| compliance can read the report without clinical access | pass |
| the review sample returns ids only | pass |

## Notable bugs caught

**A test that passed for the wrong reason — the Phase 1.5 trap again.**
`test_capture_never_blocks_a_signature` patched
`app.services.edit_burden.compute_note_burden`, but `pilot_metrics` binds
that function at import with `from … import`, so the patch never applied,
the real function ran, capture succeeded, and the assertion that no metric
row exists failed. Had the assertion been weaker the test would have passed
green while proving nothing about the safety property it exists for. Fixed
by patching where the function is *used*. This is the same shape as Phase
1.5's module-level dispatch dict defeating `monkeypatch`.

**A docstring asserting a safety property the code did not have.** The
`EncounterRating.comment` docstring said it "is therefore encrypted at rest
like any other free-text clinical field" while the column was declared as
plain `Text`. Caught on review before the migration was generated. There is
now a test that plants a patient name in a comment and reads the raw column
back.

**Phase 4.1's rotation-coverage test earned its keep.** Adding an encrypted
column made `test_every_encrypted_column_in_the_schema_is_discovered` fail —
by design, since it asserts an *exact* set. The rotation script had already
auto-discovered the column (it discovers by type, not from a list), but the
test still forced a human to confirm the coverage was intended. Exactly the
control 4.1 described, firing on the first new column since it was written.

**A mypy error surfaced by installing a dependency.** Adding `sentry-sdk` to
requirements let mypy see the real SDK signatures for the first time, which
flagged `before_send`'s type. Fixed with a *narrow* ignore on that one
argument rather than the whole `init` call, so a future mistake in any other
init kwarg — the ones carrying the PHI-safety guarantees — still fails the
type check.

## Open follow-ups

- **The threshold is calibrated against nothing.** 0.90 is a guess, labelled
  as one. The first two weeks of real signed notes should settle it, moved
  **once**, before alpha, with the version bumped.
- **The metric is not length-neutral**, measured rather than assumed:
  swapping two words scores 0.84 in a nine-word Assessment and passes
  comfortably in a forty-word one. A clinic whose notes run short will look
  like it edits more. Per-section distribution is stored so this can be
  checked against real data.
- **No dashboard.** The report is a JSON endpoint; nobody has built a page
  for it, and Phase 5.2's alert delivery still has no Sentry account behind
  it.
- **Unsafe-acceptance rate has no denominator yet.** The sampling workflow
  exists; the reviewer's verdict is not captured anywhere, so the rate is
  computed off-system until there is somewhere to record "this note was
  unsafe and was accepted".
- **Token counts are still estimated**, carried over from 5.2 — real
  per-consult cost needs a `usage` read from the vendor responses.
