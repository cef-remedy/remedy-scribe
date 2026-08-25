# 0003 — How strict is mid-visit re-consent detection?

**Phase:** 0.1 (flagged) / 2.3 (implemented) · **STATUS: OPEN — your call**

From the checklist (0.1): P0-1 says a new participant mid-recording pauses
recording until fresh consent is logged. This is a product-risk call, not an
implementation detail, so it isn't decided here — the code as of Phase 0.1
only enforces that *some* valid consent exists; it has no opinion yet on
*how* a new participant gets detected mid-visit.

**Options on the table (from the checklist):**
- **(a) Doctor flags it manually.** Simple, depends on the doctor remembering
  in the middle of a live exam.
- **(b) Trust ASR diarization to detect a new speaker.** Automatic, but
  diarization invents and merges speakers constantly — expect false pauses
  mid-consult.
- **(c) Manual flag now, revisit automation after seeing real diarization
  output from actual clinic audio.**

**Suggested default if you don't have a strong opinion yet:** (c) — there's
no Taglish diarization data from this system yet, and (b)'s failure mode
(interrupting a live medical exam on a false positive) is worse than (a)'s
(a doctor occasionally forgets to flag).

**Fill in below once decided:**

- **Decision:**
- **Why:**
- **What would change my mind:**
