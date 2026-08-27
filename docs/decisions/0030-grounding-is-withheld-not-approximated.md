# 0030 — Grounding is withheld, not approximated

**Phase:** 3 · **Decided by:** implementation · **Date:** 2026-08-27

## The problem the checklist does not name

Phase 3 has no 🧠 of its own. It has something sharper: an 📚 saying this
feature *is* the product's trust mechanism, and a ⚠️ saying notes outlive
audio. Both point at the same property, which is worth stating as a rule
because it drove every choice below:

> For a feature whose only job is proof, a **confidently wrong answer is worse
> than no answer.**

An empty grounding panel tells a doctor "check this yourself." A grounding
panel highlighting the wrong sentence tells them "this was verified" — and
they sign on the strength of it. The second failure mode is silent, and it is
the one every obvious implementation walks into.

Three places where the obvious implementation is confidently wrong:

## 1. Stored character offsets stop being true the moment a doctor edits

`Note.source_spans` holds `text_start`/`text_end` — offsets into the section
text **as generated**. P0-5 requires free editing before signing. An
insertion in the first sentence shifts every offset after it, and slicing by
stale offsets still *works*: it returns a plausible-looking substring, just
the wrong one.

Options considered:

- **Recompute offsets after each edit.** Requires re-aligning the doctor's
  prose to the model's sentence boundaries. Unreliable in exactly the cases
  that matter (a rewritten sentence), and it would produce a *guess*
  presented as provenance.
- **Clamp offsets to the new length.** Silently wrong. The worst option and
  the easiest to write.
- **Store the generated sentence text alongside each span**, then re-find it.
  Duplicates PHI (another encrypted copy per sentence) and still fails when
  the sentence is edited — which is when it is needed.
- **Chosen: verify, and withdraw grounding when the offsets no longer fit.**

`spans_fit_text` re-derives the invariant from the data itself. Generation
builds a section by joining per-sentence strings with a single space (see
`haiku.py:_build_section`), so slicing the current text by the stored spans
and re-joining with a space must reproduce the current text exactly. Any
insertion, deletion or reordering breaks it. No extra storage, no migration,
and no ordering assumptions.

When it fails, the client renders the section as plain text and says *why*.
That is the honest outcome: the doctor is told the links no longer line up,
rather than shown links that no longer line up.

### A second, subtler flag

A **same-length substitution** leaves the offsets structurally valid — and
they genuinely are: the span still delimits that sentence. But the words are
now the doctor's, so presenting a transcript passage as "the source of this
line" would be false.

So `edited_since_generation` is reported separately, derived from a
`NoteRevision` existing for that section. Two flags because there are two
questions: *do these offsets still delimit this text?* and *are these still
the model's words?* Collapsing them would either over-report staleness
(losing grounding on a typo fix) or under-report it (claiming provenance for
a rewrite).

Deliberately an `EXISTS` on revisions, not a comparison against the earliest
revision's `previous_text`: reconstructing "the original" means ordering
revisions by timestamp, and decision 0027 already recorded that identical
`created_at` values make that ordering non-deterministic. A yes/no question
about PHI text also has no business decrypting any of it.

## 2. The database's belief about audio is not evidence

`ensure_bucket_configured` installs a lifecycle rule with
`Expiration: {Days: audio_retention_days}`. Object storage deletes recordings
on its own schedule, and **nothing writes back to the encounter row.** So
`audio_object_key` set with `audio_deleted_at` NULL does not mean the bytes
exist.

Reading the row and offering a play button on the strength of it produces
exactly the failure the Phase 3 heads-up warns about: a control that does
nothing, and an opaque storage 404 in the console.

So `_audio_state` asks storage — a `HEAD` before any play button is offered —
and when the object is gone while the row still claimed otherwise, it stamps
`audio_deleted_at`. The lifecycle rule is the only thing that could have
removed it, so recording retention expiry is a statement of fact, and the
`HEAD` is then paid once rather than on every note open.

**Storage being unreachable is its own state, not rounded up to "deleted."**
"We could not check" and "it is gone" are different facts, one of them
permanent, and guessing the harsher one is still a guess. It also must not
stamp a deletion that never happened.

### The reason matters more than the fact

Audio absence is reported as one of five states, because they mean different
things to a clinician:

| state | what the doctor is told |
|---|---|
| `available` | playback offered |
| `never_recorded` | no recording was ever uploaded |
| `withdrawn` | deleted **at the patient's request** (P0-1) |
| `expired` | the retention period elapsed |
| `unreachable` | not a deletion; try again shortly |

`withdrawn` and `expired` are observably identical — no object either way —
but a withdrawal is a legal event under the Data Privacy Act and RA 4200, not
the passage of time. The consent ledger already records it, so the
distinction costs one `EXISTS` and is worth stating plainly.

## 3. The transcript is not shipped wholesale to render a highlight

`resolve_grounding` returns only the **cited** segments plus one neighbour
either side, not the whole transcript.

The transcript is arguably the most sensitive artifact in the system —
verbatim, including whatever the doctor chose *not* to write down. A 30-minute
consult is hundreds of segments; decrypting all of them and sending them to a
browser so it can highlight three is more exposure than the feature needs.

Neighbours are included because a passage read without its surroundings is
easy to misread ("the patient answered a question" vs "the patient
volunteered this"). They are flagged `cited: false` and ranked visually below
the cited passages, because a neighbour is *not* what the line cited and must
never read as evidence.

## Playback: a window, not a file

Two properties, both enforced rather than hoped for:

- **The presigned URL is signed `Cache-Control: no-store`** and
  `Content-Disposition: inline`. P0-7 asks for playback "without permanently
  re-downloading PHI"; this keeps the bytes out of the browser's HTTP cache
  and out of the Downloads folder. Range requests go straight to object
  storage, so only the seconds actually played transfer, and the API server
  never sees the audio — the same property the presigned *upload* path has
  (decision 0013).
- **Playback stops at the passage's end**, not the recording's. A doctor asked
  to hear one line's source; running on through the consultation discloses
  more than they asked for and does it without them noticing. A 250 ms tail is
  added because `end_ms` is the ASR's last-word timestamp and the
  `timeupdate` tick is ~250 ms — cutting at exactly `end_ms` clips the final
  word, which defeats the point.

The URL is minted **on demand, from its own endpoint**, not as part of the
grounding read. A presigned URL is a live playable handle on PHI; issuing one
every time a doctor opens a note hands out a working link to a recording they
may never ask to hear. It also gets its own audit action
(`encounter.audio.playback_url`) — listening to a consultation is a larger
disclosure than reading its summary. The object key is not recorded: it is a
direct pointer to the bytes, and an audit row outlives the retention window
of what it points at.

Expiry is its own setting (300s) rather than reusing the 900s part-upload
window. An upload URL goes to a device that already holds the bytes; this one
does not. Not shorter than a few minutes, though — a browser streams via
Range requests over the URL's lifetime, so an aggressively short expiry breaks
playback mid-passage rather than improving anything.

## Grounding before editing, in the UI

The 2.6 review screen made editing frictionless: a textarea per section,
saved on blur. You cannot click a line *inside* a textarea to ask where it
came from, so something had to give.

Sections now render as clickable lines by default and swap to a textarea on
an explicit "Edit this section." That ordering is the decision, not a
workaround: the first pass over an AI draft should be verification, which is
the same reason P0-4 specifies APSO. Making "check this line" the default
gesture and "rewrite it" the deliberate one matches what the doctor is
accountable for when they sign.

A signed note renders as lines only — there is nothing to edit, and checking
it is the one useful thing left to do with a permanent record.

Interaction is the two taps the checklist specifies (highlight, then audio)
rather than one. Playing a recording out loud is not something to trigger by
accident in a room with a patient in it.

## A line that cites nothing is surfaced, not hidden

A generated sentence can end up with no citations — the model cited nothing,
or cited IDs that were dropped because they were not among the segments
actually sent (`haiku.py` verifies rather than trusts). That is the line most
worth a second look, so it is marked in the note itself (a wavy underline)
and the panel says so in words. Dropping the span would render it as ordinary
prose, indistinguishable from a well-sourced line.

## What would change my mind

- If edit-burden data (Phase 6) shows doctors routinely make small typo fixes
  that currently withdraw grounding for a whole section, span-level rather
  than section-level validity would be worth the complexity — `spans_fit_text`
  is already per-section only because that is the granularity generation
  writes.
- If a browser is found where seeking a MediaRecorder-produced WebM (which
  reports `duration = Infinity`, since the header carries no duration) is
  unreliable, playback would need a server-side clipped-range endpoint instead
  — at the cost of the API server seeing the audio again.
