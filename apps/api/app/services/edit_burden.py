"""What counts as a "minor edit" (Phase 6, decision 0039) — the definition
the PRD's headline quality target is measured against.

> "≥70% of signed notes require only minor edits."

That sentence decides whether the pilot passes, so the definition behind it
is a real decision rather than a formula choice. Four things below are
deliberate, and three of them are places where the obvious implementation
gives a confidently wrong number.

## 1. A pure distance threshold is unsafe, and this is the whole point

The tempting definition is "changed fewer than N% of characters". Under it,
`5mg` → `50mg` is a **one-character** edit: maximally minor, and the most
dangerous correction a doctor can make. `no chest pain` → `chest pain` is a
three-character deletion that inverts a clinical finding.

So similarity alone is never sufficient here. An edit is minor only if it
is *both* small **and** clinically inert. `_safety_critical_changes` looks
for the specific categories where a tiny diff carries large meaning —
numbers and doses, negations, and the words that bind them — and any hit
disqualifies the section regardless of how few characters moved.

This is the checklist's own framing ("clinically-weighted: a changed dose
counts more than a rephrased sentence") implemented at the only altitude
that is honest without a clinical NLP model: it does not claim to
understand the edit, it claims to **recognise the edits it must not call
minor**. False positives (flagging a rephrase that happens to move a
number) cost a slightly pessimistic metric. A false negative is a changed
dose reported as "the draft was basically right".

## 2. The measurement is generated → signed, not the sum of the revisions

`NoteRevision` rows record every edit, saved on blur (Phase 2.6). A doctor
who types a word, deletes it, and retypes it produces three revisions and
**zero** net change. Summing per-revision distances would score that as
heavy editing, which is the opposite of true.

What the target is actually asking is: *how far is the note the doctor
signed from the note the model drafted?* So this compares two texts — the
generated text and the signed text — and the revisions are used only to
recover the first of them.

## 3. Recovering the generated text without trusting timestamps

The generated text is the `previous_text` of the *earliest* revision for a
section. "Earliest" is the trap: decision 0027 already recorded that
identical `created_at` values make timestamp ordering non-deterministic,
and Phase 3 deliberately avoided depending on revision order for exactly
this reason.

So the root is found structurally instead: **the generated text is the
`previous_text` that never appears as any `new_text` for that section.**
Edits form a chain A→B→C; only A is never anyone's output. That is
ordering-independent and needs no new column.

It has one genuine ambiguity — a doctor who edits and then reverts
(A→B, B→A) makes every value both an input and an output — and that case
is detected and reported as `ambiguous` rather than guessed at. A revert
means the net change is nil anyway, so the honest answer is available
without resolving the order.

## 4. The result is frozen, with the definition that produced it

"A metric redefined mid-pilot tells you nothing." The defence is not to
refuse to ever change the definition, but to make a change **visible**: every
computed row stores `DEFINITION_VERSION`, and changing the rules here means
bumping it and recomputing into new rows rather than silently rewriting the
old ones. A pilot report can then say which definition it is quoting, and a
mixed-version report is detectable instead of merely wrong.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.note import Note, NoteRevision

#: Bump when any rule below changes. Stored on every computed row so a
#: report can state which definition it is quoting, and so a definition
#: change produces new rows rather than rewriting history.
DEFINITION_VERSION = "edit-burden-v1"

SECTION_NAMES = ("assessment", "plan", "subjective", "objective")

#: Word-level similarity at or above which a section counts as minor,
#: *given* it is also clinically inert.
#:
#: 0.90 is a starting point, not a measurement, and saying so matters more
#: than the number: nobody has pilot data yet, so this is calibrated
#: against nothing. It is deliberately generous — the failure mode that
#: would embarrass this metric is calling real rewriting "minor", and a
#: high bar guards that direction. What would settle it is the first two
#: weeks of real signed notes: compute the distribution, look at where
#: clinicians' own sense of "I barely touched it" falls, and move the
#: threshold once, before alpha, with the version bumped.
#:
#: ⚠️ **A ratio penalises short sections**, and this was measured rather
#: than reasoned about: swapping two words in a nine-word Assessment
#: scores 0.84 and fails, while the same two-word swap in a forty-word
#: Assessment scores well above the bar. The same absolute edit is a
#: bigger fraction of a shorter section. That is arguably correct — two
#: words *is* a large share of a short note — but it means the metric is
#: not length-neutral, and a clinic whose notes run short will look like
#: it edits more. Worth checking against the real length distribution
#: before the threshold is finalised, and a reason to report the
#: per-section distribution rather than only the headline rate.
MINOR_SIMILARITY_THRESHOLD = 0.90

# --- the clinically-inert test -------------------------------------------

#: Any numeric token: doses, frequencies, durations, vitals, ages. Matched
#: loosely on purpose — "5", "5.0", "5mg", "0.5" all count — because the
#: question is "did a quantity change?", not "is this well-formed".
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

#: Negations and hedges whose appearance or disappearance flips meaning.
#: `no chest pain` → `chest pain` is three characters and a different
#: patient. "denies" and "without" are the same inversion in clinical prose.
_NEGATIONS = frozenset(
    {
        "no",
        "not",
        "none",
        "never",
        "denies",
        "denied",
        "without",
        "absent",
        "negative",
        "unremarkable",
        "afebrile",
        "nil",
        "wala",  # Filipino: "none" — P0-3 keeps Taglish verbatim, so the
        "hindi",  # negation vocabulary has to as well, or a negation flip
        "walang",  # in the patient's own words reads as inert.
    }
)

#: Units that make a neighbouring number a dose rather than a count.
#: Present so a changed unit (mg → mcg, a 1000x error) is caught even when
#: the digits are identical.
_DOSE_UNITS = frozenset(
    {
        "mg",
        "mcg",
        "g",
        "kg",
        "ml",
        "l",
        "iu",
        "units",
        "unit",
        "tab",
        "tabs",
        "tablet",
        "tablets",
        "cap",
        "caps",
        "capsule",
        "capsules",
        "mmhg",
        "bpm",
        "mg/dl",
        "mmol/l",
        "puff",
        "puffs",
        "drop",
        "drops",
    }
)

_WORD = re.compile(r"[\w/.,-]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _multiset(items: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def _removed_and_added(before: str, after: str) -> tuple[dict[str, int], dict[str, int]]:
    """Word multisets that left and entered, ignoring pure reordering.

    Multisets rather than sets: `5mg twice` → `5mg 5mg` has to register the
    duplication, and a set would see no change at all.
    """
    b, a = _multiset(_words(before)), _multiset(_words(after))
    removed = {w: n - a.get(w, 0) for w, n in b.items() if n > a.get(w, 0)}
    added = {w: n - b.get(w, 0) for w, n in a.items() if n > b.get(w, 0)}
    return removed, added


def _safety_critical_changes(before: str, after: str) -> list[str]:
    """The categories of change that must never be called minor.

    Returns human-readable reasons rather than a bool, because a reviewer
    reading a flagged note needs to know *which* rule fired — "a number
    changed" and "a negation was removed" call for different scrutiny.
    """
    removed, added = _removed_and_added(before, after)
    if not removed and not added:
        return []

    reasons: list[str] = []

    # Any quantity entering or leaving. Deliberately not "the numbers
    # differ": adding a dose to a plan that had none is as clinical as
    # changing one.
    changed_numbers = sorted({w for w in list(removed) + list(added) if _NUMBER.search(w)})
    if changed_numbers:
        reasons.append(f"numeric value changed ({', '.join(changed_numbers[:5])})")

    changed_units = sorted({w for w in list(removed) + list(added) if w in _DOSE_UNITS})
    if changed_units:
        reasons.append(f"dose unit changed ({', '.join(changed_units[:5])})")

    changed_negations = sorted({w for w in list(removed) + list(added) if w in _NEGATIONS})
    if changed_negations:
        reasons.append(f"negation changed ({', '.join(changed_negations[:5])})")

    return reasons


# --- recovering what the model actually wrote ----------------------------


@dataclass(frozen=True)
class SectionBurden:
    section: str
    similarity: float
    is_minor: bool
    #: Empty when the edit is clinically inert. Non-empty disqualifies the
    #: section from "minor" no matter how small the diff.
    safety_flags: list[str] = field(default_factory=list)
    edited: bool = False
    #: True when the revision chain could not be rooted (an edit-then-revert
    #: makes every value both an input and an output). Reported rather than
    #: guessed; see this module's docstring.
    ambiguous: bool = False


@dataclass(frozen=True)
class NoteBurden:
    note_id: str
    definition_version: str
    sections: dict[str, SectionBurden]
    #: The headline: does this note count toward "≥70% require only minor
    #: edits"? True only when *every* section is minor. A note is signed as
    #: one document, and a rewritten Plan is not redeemed by an untouched
    #: Subjective.
    minor_only: bool
    #: Mean similarity across sections, for distribution analysis. Not the
    #: pass/fail input — that is `minor_only`.
    mean_similarity: float
    any_ambiguous: bool

    @property
    def safety_flagged_sections(self) -> list[str]:
        return sorted(name for name, s in self.sections.items() if s.safety_flags)


def generated_text(db: Session, note_id: str, section: str, current_text: str) -> tuple[str, bool]:
    """The section's text as the model produced it, plus an ambiguity flag.

    Returns `(text, ambiguous)`. With no revisions the current text *is* the
    generated text — the doctor never touched it — which is the common case
    and costs no reconstruction at all.
    """
    rows = (
        db.query(NoteRevision.previous_text, NoteRevision.new_text)
        .filter(NoteRevision.note_id == note_id, NoteRevision.section == section)
        .all()
    )
    if not rows:
        return current_text, False

    previous_texts = [r[0] for r in rows]
    new_texts = {r[1] for r in rows}

    # The root of the chain: an input that is nobody's output.
    roots = [p for p in previous_texts if p not in new_texts]
    if len(set(roots)) == 1:
        return roots[0], False
    if not roots:
        # Edit-then-revert: every value is both. The net change is nil, so
        # the current text is the honest answer, but the reconstruction was
        # genuinely ambiguous and the caller is told.
        return current_text, True
    # More than one distinct root means the chain forked — possible if rows
    # were written concurrently. Take none of them silently.
    return current_text, True


def compute_note_burden(db: Session, note: Note) -> NoteBurden:
    """Edit burden for one note, comparing the generated text to the text as
    it now stands.

    Intended to be called at the moment of signing, when "as it now stands"
    is final and immutable — but it is a pure read, so it can be run over
    historical notes to backfill or to re-score under a new definition.
    """
    sections: dict[str, SectionBurden] = {}
    for name in SECTION_NAMES:
        signed = getattr(note, name) or ""
        original, ambiguous = generated_text(db, note.id, name, signed)

        if original == signed:
            # Untouched sections are minor by definition and skip the
            # matcher entirely — the common case, and the cheap one.
            sections[name] = SectionBurden(
                section=name, similarity=1.0, is_minor=True, edited=False, ambiguous=ambiguous
            )
            continue

        # Word-level, not character-level: rewrapping a line or fixing
        # whitespace is not editing, and a character ratio would score it as
        # if it were.
        similarity = difflib.SequenceMatcher(None, _words(original), _words(signed)).ratio()
        flags = _safety_critical_changes(original, signed)
        sections[name] = SectionBurden(
            section=name,
            similarity=round(similarity, 4),
            is_minor=similarity >= MINOR_SIMILARITY_THRESHOLD and not flags,
            safety_flags=flags,
            edited=True,
            ambiguous=ambiguous,
        )

    mean = sum(s.similarity for s in sections.values()) / len(sections)
    return NoteBurden(
        note_id=note.id,
        definition_version=DEFINITION_VERSION,
        sections=sections,
        minor_only=all(s.is_minor for s in sections.values()),
        mean_similarity=round(mean, 4),
        any_ambiguous=any(s.ambiguous for s in sections.values()),
    )
