# 0039 — A "minor edit" is small **and** clinically inert

**Phase:** 6 · **Decided by:** implementation (deferred from 2.6 by design) · **Date:** 2026-08-29

## The question, and why it waited

> 🧠 **What counts as a "minor edit"?** The PRD's headline quality target is
> "≥70% of signed notes require only minor edits," so this definition
> literally determines whether you pass. Character-level edit distance?
> Word-level? Clinically-weighted (a changed dose counts more than a
> rephrased sentence)? Decide before alpha, write it down, and compute it
> consistently — a metric redefined mid-pilot tells you nothing.

Phase 2.6 deliberately did not answer this, on the grounds that a UI phase
should not bake in a measurement choice, and that `NoteRevision` storing
full before/after text for every edit kept every candidate definition
computable retrospectively. That held: nothing below required new data
capture, only new interpretation of data already being recorded.

The heads-up on this phase raises the stakes further, and it has aged
badly in a specific way. It says the stated mitigation for skipping the
vendor bake-off was "watch the edit-burden metric closely from day one of
internal alpha." Since it was written, **decision 0035 swapped the note
generator to a different vendor entirely, also without a bake-off, also on
the strength of watching this metric.** The mitigation is now carrying two
unvalidated vendor choices instead of one.

## Decision: similarity **and** a clinical-safety veto

An edit is minor when **both** hold:

1. word-level similarity between the generated and signed text is
   ≥ `MINOR_SIMILARITY_THRESHOLD` (0.90), and
2. the change is **clinically inert** — no number, dose unit or negation
   entered or left the text.

A note counts toward the ≥70% target only when *every* section is minor.

### Why similarity alone is not merely imprecise but unsafe

The obvious definition is a distance threshold. Under it:

| edit | character distance | what it is |
|---|---|---|
| `500mg` → `5000mg` | 1 character | a **10× overdose** |
| `500mg` → `500mcg` | 1 character | a **1000× error**, same digits |
| `no chest pain` → `chest pain` | 3 characters | an inverted finding |

Each of those is *maximally minor* by distance and among the most
consequential corrections a doctor can make. A metric that scores them as
"the draft was basically right" does not merely mis-measure — it reports
the model as most trustworthy precisely where it was most dangerous, and it
does so in the number being used to justify skipping vendor validation.

So condition (2) is a **veto**, not a weighting. No similarity score, however
high, can make a changed dose minor.

### What the veto actually claims

`_safety_critical_changes` does not claim to understand a clinical edit. It
claims to **recognise the categories it must not call minor**: quantities,
dose units, and negations — including Filipino negations (`wala`, `hindi`,
`walang`), because P0-3 keeps Taglish verbatim and a negation flip in the
patient's own words is the same inversion.

The error budget is deliberately asymmetric. A false positive — flagging a
rephrase that happens to move a number — makes the metric slightly
pessimistic, which is a number that under-sells the product. A false
negative is a changed dose reported as a trivial edit. Only one of those is
recoverable after the fact.

This is the checklist's own "clinically-weighted" option, implemented at
the only altitude that is honest without a clinical NLP model.

## Three implementation choices that would each have produced a wrong number

**Measure generated → signed, not the sum of the revisions.** Edits are
saved on blur (2.6), so a doctor who types a word, deletes it and retypes it
produces three revisions and zero net change. Summing per-revision distances
scores that as heavy editing. The target asks how far the *signed* note is
from the *drafted* one, so it compares two texts and uses revisions only to
recover the first.

**Recover the generated text structurally, not by timestamp.** The
generated text is the earliest revision's `previous_text`, and "earliest" is
the trap: decision 0027 recorded that identical `created_at` values make
revision ordering non-deterministic, and Phase 3 avoided depending on it for
exactly this reason. Instead: **the generated text is the `previous_text`
that never appears as any `new_text`** for that section. Edits form a chain
A→B→C and only A is nobody's output. Ordering-independent, no new column.

Its one genuine ambiguity — an edit-then-revert (A→B, B→A) makes every value
both an input and an output — is *reported* rather than guessed at. A revert
means net change is nil, so the honest answer is available without resolving
the order, and `reconstruction_ambiguous` lets a pilot report say how many
of its inputs were uncertain.

**Freeze the result with the definition that produced it.** "A metric
redefined mid-pilot tells you nothing." The defence is not refusing to ever
change the definition — it is making a change *visible*. Every row stores
`DEFINITION_VERSION`, keyed `(note_id, definition_version)`, so re-scoring
writes new rows beside the old ones and a mixed-version report is detectable
rather than merely wrong.

Freezing is also a hard requirement rather than a nicety: **Phase 4.4's
retention purge deletes note revisions** while the signed note is a
permanent record. A metric that depended on live revisions would quietly
become uncomputable partway through the pilot.

## The threshold is a guess, and is labelled as one

0.90 is calibrated against nothing — there is no pilot data yet. It is
deliberately generous, because the failure that would embarrass this metric
is calling real rewriting "minor".

⚠️ **A ratio penalises short sections, and this was measured rather than
reasoned about.** Swapping two words in a nine-word Assessment scores 0.84
and fails; the same two-word swap in a forty-word Assessment passes
comfortably. That is arguably correct — two words *is* a large share of a
short note — but the metric is not length-neutral, and a clinic whose notes
run short will look like it edits more. This is why the per-section
distribution is stored and reported, not just the headline rate.

What settles it is the first two weeks of real signed notes: compute the
distribution, compare it against clinicians' own sense of "I barely touched
it", and move the threshold **once**, before alpha, with the version bumped.

## Capture must never block a signature

`capture_note_quality` swallows its own exceptions — a deliberate inversion
of this codebase's fail-loudly instinct. Signing is the legal act that makes
a doctor accountable (P0-5); measurement is an observer of it. A bug in a
similarity ratio must not refuse a signature or roll one back. The metric is
recomputable from the note; a refused signature in a consultation room is
not.

The compensating control is `coverage` (measured ÷ signed) on the report:
silent loss becomes visible instead of quietly biasing the headline rate.

## Two metrics are reported with their limits attached

**"Correctly filed" is a caught-error rate, not a correctness rate.** The
system observes *rejected* filings — the 409s when a confirmed patient does
not match the encounter (P0-6). It cannot observe a note filed to the wrong
patient that the doctor confirmed anyway, because at that point every check
agrees. Reporting it as the true rate would be the most flattering possible
reading of the data. The real figure needs the weekly manual review.

**Documentation time is split in two.** Encounter-creation-to-signature is
what compares to the week-0 paper baseline; generation-to-signature is the
part the product controls. A doctor who leaves a note open over lunch
inflates the first and not the second, and conflating them makes the
headline unusable.

## What would change my mind

- **Real distribution data.** If the first fortnight shows the threshold
  sitting in a dense part of the distribution, small calibration differences
  will swing the headline rate — that argues for reporting a distribution
  and a median rather than a single pass/fail rate, and the per-section JSON
  is stored so that change costs nothing.
- **Clinicians disagreeing with the veto.** If doctors routinely correct
  ASR-mangled numbers that were never clinically wrong (a misheard age), the
  veto will read as noise. The fix is not to weaken it but to separate
  "changed a number" from "changed a number in a Plan", which the stored
  per-section data already supports.
- **A structured medication field.** Most of the veto's work is
  reconstructing drug and dose from prose. If notes ever carry structured
  medications, comparing those directly would be strictly better than
  pattern-matching the text.
