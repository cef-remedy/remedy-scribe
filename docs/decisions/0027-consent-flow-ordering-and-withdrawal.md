# 0027 — Consent flow: strict ordering, and what withdrawal actually does

**Phase:** 2.3 · **Decided by:** implementation (P0-1's own wording forced most of it) · **Date:** 2026-08-27

## 1. The ordering is stricter than it first reads, and it is load-bearing

P0-1's first two bullets, together, pin the sequence exactly:

> - "when no consent record exists... the app blocks recording and presents
>   the consent script (Filipino + English) **before anything is captured**"
> - "**Given consent is given**, when recording starts, then the spoken
>   exchange is captured as the **first segment** of the audio file"

My first reading was that the doctor starts recording, reads the script, and
the recorded asking becomes segment 1. That satisfies the second bullet and
**violates the first** — it captures audio before consent exists.

The only sequence satisfying both: capture the roster → read the script aloud
→ log the outcome to the ledger → *then* start recording → the doctor speaks
a short confirmation, which lands as segment 1. So **the consent screen never
touches the microphone**, and the recording screen shows the confirmation
prompt only once capture is actually running. There is a smoke assertion for
each half, because "no `getUserMedia` on this screen" is exactly the kind of
property that regresses silently.

A consequence worth naming: the *asking* is not in the recording, only the
doctor's confirmation that it happened. If Legal wants the patient's own
spoken "yes" on tape, that is a different design — it would require recording
before consent is logged, and someone with authority would have to decide
that the act of asking permission is itself covered. That is not an
engineering call.

## 2. Decline gates on the script being *presented*, not merely available

The decline button is unreachable until the doctor advances past the roster
step. P0-1 requires the app to *present* the script, and logging a decline
the patient was never read is recording a decision they were not informed
enough to make.

This costs a tap in the case where a patient refuses immediately. Accepted:
a decline is a response to the script, and the ledger entry claims the script
was delivered in the recorded `script_language`.

## 3. Withdrawal: three actions, ordered so the least important cannot undo the most

P0-1: "processing stops and the associated audio is queued for deletion
without undue delay."

**Server** (`services/consent.py:handle_withdrawal`), in this order:

1. **The ledger entry is committed first**, by the route, before anything
   else runs. It is the legal record and must survive everything below.
2. **The retention clock is set to now.** The durable backstop — whatever
   happens to step 3, the encounter becomes eligible for the retention job
   (Phase 4.4) immediately instead of sitting for 90 days.
3. **An immediate object delete is attempted**, best-effort. `delete_object`
   returns a bool rather than raising, because a withdrawal must not fail
   because object storage was briefly unreachable.

**Client** (`routes/Record.tsx:onWithdraw`), also ordered deliberately: stop
capturing → delete the local chunks → tell the server. If the network call
fails, the audio is already gone from the laptop, so the failure mode leaves
*less* data behind rather than more.

**It does not try to kill a running Celery task**, and the UI says so. The
checklist's own heads-up is explicit that you cannot reliably abort a task
mid-flight, so the design is "stops at the next checkpoint" — which Phase 0.1
already guarantees by re-checking consent at upload confirmation and at the
head of `transcribe_encounter`. The response carries
`pipeline_will_stop`/`audio_deleted`/`nothing_to_delete` so the doctor is told
what actually happened, and a smoke assertion checks the wording says "next
stage boundary, not instantly". A doctor standing in front of a patient who
just asked to stop being recorded should not be relaying a guess — and this is
also what Legal will be told the system does.

## 4. The script text is a placeholder, isolated so replacing it is one edit

`lib/consent-script.ts` carries both language versions as data, with a banner
in the UI itself stating it has not been cleared by counsel. The PRD lists RA
4200 clearance as an Open Question owned by Legal and marked **blocking** —
"recording feature cannot launch without this". Keeping the text in one file
rather than inline in components means counsel's version is a single edit.

The `purposes` list is deliberately narrower than a generic consent form
would be: it names audio recording, transcription, and draft-note generation,
and nothing else. The PRD's Non-Goals exclude prescriptions and EMR
integration, and a consent form claiming broader purposes than the system has
would be both inaccurate and a liability.

## A latent issue found while testing, not fixed here

`current_consent_state` folds the ledger ordered by `created_at`. If two
entries for one encounter ever share a timestamp exactly, their order — and
therefore the computed consent state — is undefined. `Clinician.id` and the
entry id are random UUIDs, so there is no meaningful tiebreak available.

In production this needs two HTTP requests to land in the same microsecond,
which is not realistic. But the fold answers a legal question, and
"non-deterministic" is a bad property for one. The cheap fix is a monotonic
sequence column on the ledger. Not done here to keep 2.3 scoped; recorded so
it is a known gap rather than a surprise. It surfaced because a test helper
offset timestamps *forwards* from now, which put a seeded grant after a
route-written withdrawal and flipped the state — a fixture bug that happened
to demonstrate the real fragility.

## What would change my mind

- If counsel decides the patient's spoken agreement must be on the recording,
  §1's ordering has to change and someone with authority must sign off that
  recording the asking is lawful. Engineering should not resolve that by
  choosing a reading of P0-1.
- If withdrawals during an in-flight transcription turn out to be common,
  "stops at the next checkpoint" may not be fast enough for Legal, and the
  answer is more checkpoints (e.g. a consent re-check between ASR and note
  generation), not an attempt at instant abort.
