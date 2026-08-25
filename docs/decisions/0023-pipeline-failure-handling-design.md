# 0023 — Pipeline failure handling: two mechanisms, not one; enum scoped to what actually needs it

**Phase:** 1.5 · **Decided by:** implementation (the checklist named the requirements; this phase's own 📚 Understand-first note already named the two-mechanism split, this record is what followed from applying it) · **Date:** 2026-08-25

**Decision, in three parts:**

1. **`EncounterPipelineStatus` gains exactly two new terminal members** —
   `transcription_failed`, `generation_failed` — not the three decision
   0011 anticipated (`upload_failed` too). Upload failures are
   synchronous: the phone's own `/upload/complete` request gets a 409/502
   directly and can retry immediately, so there is never a row sitting
   in an ambiguous "upload failed" state waiting to be discovered later.
   The other two run inside a Celery task nobody is watching in real
   time, which is the actual property that makes a terminal status worth
   having. Adding `upload_failed` anyway would be exactly the kind of
   speculative, unused-but-plausible member decision 0011 already argued
   against.

2. **Dead-lettering and the stuck-sweep are two separate mechanisms
   because they catch two different failures, not because one wasn't
   thorough enough.** Dead-lettering (inside each task's `except` block,
   once `self.request.retries >= self.max_retries`) only fires for a
   task that actually ran and raised. It cannot catch a task that never
   ran at all — the broker down, or the worker pool at zero, at the
   exact moment `run_pipeline` fired. `sweep_stuck_encounters` (Celery
   Beat, every 5 minutes) catches that other case by comparing
   `pipeline_updated_at` against a configurable staleness threshold
   instead of by catching an exception, because there's no exception to
   catch. Building only the first would leave exactly the failure mode
   this phase's own 📚 note calls "the real hard part."

3. **`/retry` re-runs only the failed stage, not the whole pipeline from
   scratch.** A `GENERATION_FAILED` encounter already has a real,
   persisted transcript — re-running transcription too would silently
   re-pay for a real ASR call whose output the first attempt already got
   right, for a system whose own PRD sets a per-consult cost target.
   `run_note_generation` (new, mirrors `run_pipeline`) exists specifically
   so both `/retry` and `sweep_stuck_encounters` can re-kick just the note
   stage without another transcription pass.

**Why re-kicking a "stuck" encounter is safe, not reckless:** both
`transcribe_encounter` and `generate_note` already no-op if the work they
would do already exists (checked at the top of each task, predating this
phase). `sweep_stuck_encounters` re-invoking one of them on an encounter
that merely looked stuck — a slow worker, not a dead one — does nothing
harmful; it finds nothing left to do and returns immediately. The sweep
only needs to be safe to be wrong occasionally, not perfectly accurate
about what's *actually* stuck.

**A related, smaller decision folded in here: `pipeline_updated_at` is
its own column, not a generic `updated_at` with `onupdate=func.now()`.**
A generic column would also fire when `link_patient` changes
`patient_id` — an edit with nothing to do with pipeline progress — and
the sweep would then be comparing against the wrong signal. Set
explicitly, and only, at each real pipeline transition (including the
two new failure states and the reset-to-zero on `/retry`), it means
exactly "when did this row's pipeline last move."

**What would change my mind:** if Phase 2's offline queue (P0-2) turns
out to produce a real class of stuck-forever uploads after all — e.g. a
device that completed all S3 parts but crashed before calling
`/complete`, and never retries on relaunch — that's a client-side gap in
2.4's queue durability, not a server-side pipeline status this enum
should grow a member for; the fix belongs in the client's own retry
logic, not here.
