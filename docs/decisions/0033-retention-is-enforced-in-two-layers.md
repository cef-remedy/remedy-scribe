# 0033 — Retention is enforced in two layers, and deletes derived PHI too

**Phase:** 4.4 · **Decided by:** implementation · **Date:** 2026-08-28

## The 🧠 this resolves, and on whose authority

Phase 4.4 carries a 🧠 addressed to the user: *"Celery Beat, a cron job, or
bucket lifecycle rules?"* This is an **implementation-level resolution of a
question the checklist flagged for the user** — recorded here so it is visible
as such, and so overturning it is a matter of reading one file rather than
re-deriving the reasoning.

It is resolvable at this level because the checklist's own text already
narrows it to one candidate answer ("most likely you want both") and because
the part that genuinely is the user's — **how long** retention lasts — is not
touched here. `audio_retention_days` remains the single configurable value,
still 90, still owned by Legal/Compliance, and the PRD's open question about
its real value stays open. What follows decides only *what mechanism enforces
whatever number they pick.*

## The decision

**Both, with a clear division of responsibility:**

| layer | deletes | runs |
|---|---|---|
| bucket lifecycle rule (already in `storage.ensure_bucket_configured`) | the audio objects | inside object storage, always |
| `retention.sweep_expired_retention` (Celery Beat, hourly) | transcripts, note revisions, and the `audio_deleted_at` write-back | in the app, when the app is up |

Not a cron job. The application already runs a Beat process for
`sweep-stuck-encounters` (Phase 1.5), so a second periodic task costs one
dictionary entry and no new infrastructure, no new deploy artifact, and no
second place where "how do I run this thing on a schedule" is answered. A cron
entry would need its own container, its own Python environment, its own
database URL, and its own answer to "did it run last night?" — for a job the
scheduler we already operate can run.

## Why one layer is not enough, in either direction

**Lifecycle-only fails because the bucket cannot see Postgres.** The
transcript is the artifact this phase most needs to reach: it is verbatim,
includes what the doctor chose *not* to write down, and lives entirely in a
Postgres row. `Transcript.retention_expires_at` has been written on every
transcript since Phase 1.2 and read by nothing. No S3 lifecycle rule will ever
touch it.

**Application-job-only fails because an application job is a promise.**
Retention is a compliance control under the Data Privacy Act. A control that
holds only while a Celery Beat container is scheduled, healthy, and pointed at
the right broker is a control with an availability dependency, and the failure
is silent — nothing tells you the audio you promised to delete 40 days ago is
still sitting in the bucket. The lifecycle rule has no such dependency: it is
enforced by the storage layer whether this app is running, deployed, or
correct. For the single most sensitive artifact in the system, that is the
guarantee worth having.

So the raw recording gets the guarantee that does not depend on us, and the
derived rows get the only mechanism that can reach them.

The overlap on audio is deliberate, not redundant. The job deleting audio
first is what lets it stamp `audio_deleted_at`, and Phase 3 already documented
what happens when nothing does: `_audio_state` has to pay a `HEAD` against
object storage on every note open to find out whether the bytes are still
there. Neither layer is load-bearing for correctness alone; each covers the
other's failure mode.

## Hourly, and why not any other number

The neighbouring Beat entry runs every 5 minutes because it is chasing an
encounter a doctor is waiting on. This one is not, and two facts bracket it:

- `audio_retention_days` gives the policy **day** granularity. An hour of lag
  against a 90-day clock is invisible; a 5-minute interval would buy nothing
  and pay for it 288 times a day.
- The job is also the **backstop for a withdrawal whose immediate delete
  failed** (object storage briefly unreachable — `handle_withdrawal` is
  best-effort by design). P0-1 says "without undue delay." A nightly cron
  would turn a patient's withdrawal into an up-to-24-hour wait for the derived
  rows, which is exactly the sentence you do not want read back to you.

An hour is the longest interval that still reads as "without undue delay" and
the shortest one the retention policy can actually tell apart.

## What gets deleted, and the boundary that must not move

A **signed `Note` is a permanent medical record.** Nothing in
`app/tasks/retention.py` touches the `notes` table, in any code path, under
any reason, forced or not. There is a test asserting that a note signed 400
days ago survives a purge that removes its audio, transcript and revisions,
with its signature and its text intact.

`NoteRevision` rows are a different thing wearing similar clothes: they are
the *drafting history* — the raw material for Phase 6's edit-burden metric —
not the record. They expire.

The consent ledger is never touched either. It is append-only at the database
level, it is the legal record of the withdrawal that may have triggered the
deletion, and it is what grounding reads to explain *why* audio is gone.

## The trap: deleting revisions makes the grounding UI lie

This is the part that would have been a silent bug.

`grounding.py` derives `edited_since_generation` from a `NoteRevision` merely
*existing* for a section (decision 0030, §1). Delete the revisions while the
transcript survives, and a section the doctor rewrote flips back to reporting
"these are the model's words" — with real transcript passages still available
for the UI to highlight as its source. That is a confidently wrong answer
about provenance, on a screen whose only job is proof, which decision 0030
argues is worse than showing nothing.

So the invariant is: **revisions are only ever removed in the same purge that
removes (or has already removed) the transcript.** At that point grounding
reports `TranscriptState.EXPIRED`, returns no segments, and has nothing left
to mis-attribute. Two tests hold this: one that the revision survives while
its transcript does, and one that walks the same note through both states and
asserts the grounding output at each.

The audio signal survives the job untouched for a different reason:
`_audio_state` distinguishes `WITHDRAWN` from `EXPIRED` by asking the consent
ledger, not by anything this job writes. Stamping `audio_deleted_at` is
therefore reason-neutral — a withdrawal still reads as a withdrawal, and a
time-expiry as an expiry. Also asserted, on two encounters in identical
observable states.

## Withdrawal: immediate, and a backstop for when immediate fails

P0-1 requires that on withdrawal "processing stops and the associated audio is
queued for deletion without undue delay." `handle_withdrawal` (Phase 2.3)
already does the audio half and stamps the retention clock to now. It does not
reach the transcript — and the transcript is derived PHI from a recording the
patient has just asked to be rid of.

`purge_withdrawn_encounter` **wraps** `handle_withdrawal` rather than
reimplementing it: the ledger handling, the clock stamp, and the
`WithdrawalOutcome` the API reports back to the doctor all stay where they
are; the derived-data purge is added around them. It returns the same
`WithdrawalOutcome`, so the consent route adopts it by changing which function
it calls and nothing else.

Two things make this robust rather than merely present:

- The sweep **also treats a withdrawn encounter as due**, keying off the
  consent ledger rather than the transcript's own 90-day clock. So even before
  the route adopts the immediate path, a withdrawal's derived rows are
  collected within the hour.
- That ledger read goes through `current_consent_state`, so a withdrawal
  followed by **re-consent** does not delete anything. The ledger is a fold,
  not a latch; treating any historical withdrawal as standing permission to
  delete would destroy PHI the patient has since agreed to.

## Smaller choices, stated so they are not mistaken for accidents

**A failed storage delete does not stamp `audio_deleted_at`.**
`storage.delete_object` returns a bool by design (Phase 2.3). Stamping on
failure would make grounding report EXPIRED for a recording still sitting in
the bucket, and no later sweep would retry it — the row would have dropped out
of the due-query. Instead the row is left alone and the next sweep retries.

**The audit trail records that a deletion happened, never what was deleted.**
One row per artifact class (`encounter.audio.delete`,
`encounter.transcript.delete`, `note.revisions.delete`), each carrying the
reason and nothing else. No object key — it is a direct pointer at PHI bytes,
and an audit row outlives the retention window of what it points at, the same
reasoning that keeps the key out of `encounter.audio.playback_url` (0030). No
note or transcript text. `audit.record` is called with the deletion still
pending in the session, so the deletion and the record of it commit together;
an audit trail that can commit without its deletion is one that can lie.

**A NULL retention clock is never expired.** It means "no clock was ever set
for this row," not "expired long ago." Failing open here would delete rows the
policy never covered.

**The sweep is batched at 500 encounters.** Deletion talks to object storage
one key at a time; an unbounded first run against a long-neglected database
would hold a transaction open for as long as S3 takes to answer N times.
Everything the sweep purges stops matching its own due-query, so a backlog
drains over the following hours instead. Deliberately a module constant, not a
setting: it is an operational safety valve, not a policy knob, and inventing
config for it would blur the one knob Compliance actually owns.

**The scheduled sweep writes a NULL `actor_clinician_id`.** Nobody triggered
it. Inventing a service-account identity to blame would make the log less
true, not more complete. The withdrawal path passes the real clinician,
because a withdrawal does have a human behind it.

## What would change my mind

- **If Legal fixes a retention period shorter than a few days**, hourly stops
  being obviously fine and the lag becomes a meaningful fraction of the
  policy. The interval would need to shorten, and `audio_retention_days`
  would need to become hours.
- **If per-encounter retention overrides appear** (a research consent, a
  medico-legal hold, a jurisdiction with a different clock), the due-query's
  two clocks become a policy resolution step, and that logic belongs in a
  service module rather than inside a Celery task.
- **If the bucket lifecycle rule turns out not to be trustworthy on the
  deployment target** — MinIO's lifecycle support has already surprised this
  codebase once (decision 0014), and `ensure_bucket_configured` is
  swallow-and-warn by design, so a production deploy whose IAM role cannot set
  lifecycle policy would silently have *no* storage-layer backstop. If that is
  the deployment shape, the application job stops being the second layer and
  becomes the only one, which raises the bar on monitoring it.
- **If `TranscriptState` gains a `WITHDRAWN` member**, the withdrawal path
  should stop being reported to doctors as a plain expiry. Today grounding's
  transcript ladder has three rungs and no way to say "deleted at the patient's
  request" — the audio ladder carries that reason and the transcript one does
  not. It is a gap in what the UI can express, not a wrong signal from this
  job, but it is the first thing worth fixing about this area.
