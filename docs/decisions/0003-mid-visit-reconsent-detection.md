# 0003 — How strict is mid-visit re-consent detection?

**Phase:** 0.1 (flagged) / 2.3 (implemented) · **Decided by:** eliminated by decision 0018, not chosen · **Date opened:** 2026-08-25 · **Date closed:** 2026-08-27

From the checklist (0.1): P0-1 says a new participant mid-recording pauses
recording until fresh consent is logged. This was flagged as a product-risk
call, not an implementation detail, so it wasn't decided at the time — the
code as of Phase 0.1 only enforced that *some* valid consent exists; it had
no opinion on *how* a new participant gets detected mid-visit.

**Options on the table (from the checklist):**
- **(a) Doctor flags it manually.** Simple, depends on the doctor remembering
  in the middle of a live exam.
- **(b) Trust ASR diarization to detect a new speaker.** Automatic, but
  diarization invents and merges speakers constantly — expect false pauses
  mid-consult.
- **(c) Manual flag now, revisit automation after seeing real diarization
  output from actual clinic audio.**

---

## Decision: (a) manual flag — by elimination, not by preference

**Decision:** the doctor flags a new participant manually. The recording
screen has a "Someone joined — pause" control; pressing it pauses capture
immediately and recording cannot resume until a fresh `given` ledger entry
naming the new roster has been written (`routes/Record.tsx`,
`lib/consent.ts:reconsent`).

**Why:** this stopped being a judgement call. **Decision 0018 replaced the
ASR vendor with Groq-hosted Whisper large-v3, which has no diarization
capability at all** — not weaker diarization, none. Option (b) is therefore
not a trade-off with an unattractive failure mode; it is unbuildable, because
there are no speaker labels for it to read. Option (c) is likewise moot in
its stated form: it proposed revisiting "after seeing real diarization
output", and there is no diarization output to see.

That leaves (a) as the only implementable option. Worth being explicit that
this is elimination rather than endorsement — the original concern stands
unaddressed: a doctor mid-examination may simply forget to flag, and nothing
in the system will catch that. What the design does instead is make the
*consequence* of remembering cheap and the *state* honest:

- The pause is the compliance action and happens **before** any network call,
  so a flaky connection cannot leave capture running while consent is
  unresolved.
- Resuming is gated on the ledger write actually succeeding, not on the
  doctor's assertion that it did. Resuming without an entry would leave the
  new participant unconsented, which is precisely what the pause exists to
  prevent.
- The paused state is visually distinct but still unmistakably a live
  session. A paused recorder that looks like no recorder is how someone gets
  recorded without realising it.

**What would change my mind:** if a real diarization component is ever added
back — decision 0018 lists three routes to one — then (b) becomes buildable
and (c) becomes the sensible sequence: keep the manual flag, run diarization
in shadow mode, and compare its new-speaker events against the doctor's
flags on real Taglish clinic audio before letting it interrupt anything. The
original worry about false pauses interrupting a live medical exam is still
the right worry, and it argues for shadow-mode validation rather than
straight adoption.

A cheaper partial mitigation, available without diarization: prompt the
doctor to confirm the roster at the *end* of the recording, before the note
is filed. It cannot pause anything retroactively, but it would surface
"actually, someone came in halfway through" while the encounter is still
fresh, and that is a real disclosure the ledger would otherwise never get.
Not built; noted as the obvious next increment if forgetting turns out to be
common in the pilot.
