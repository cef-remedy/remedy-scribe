# 0028 — Upload queue: what "confirmed" means, and where the totals come from

**Phase:** 2.4 · **Decided by:** implementation · **Date:** 2026-08-27

## 1. Local audio is deleted on *pipeline* confirmation, not on `upload/complete`

P0-2 says local audio goes only after the server confirms receipt **and**
that note generation has begun. The checklist's heads-up is sharper: *"the
confirmation the device waits for should be about the pipeline, not the
bytes."*

`upload/complete` returning 200 confirms only that S3 holds the object and a
Celery chain was enqueued. A worker that never runs — broker down, worker
pool at zero, the exact scenario Phase 1.5's stuck-sweep exists for — leaves
`pipeline_status` at `uploaded` indefinitely. Deleting on the 200 would then
destroy the only copy of a consultation whose processing never started.

So the queue has a distinct `uploaded → confirmed` step that polls
`GET /encounters/{id}` (added in this phase) and only advances when
`pipeline_status` is `transcribed` or `note_generated`. `transcribed` is the
first status that proves work happened, and it is also the point at which
the transcript exists server-side, so the audio stops being the sole record
of what was said.

**Terminal server failures keep the local copy.** If the encounter reaches
`transcription_failed`, `generation_failed`, or `blocked_no_consent`, the
entry goes to `failed` and the audio stays on the laptop — it may be the only
copy, and Phase 1.5's `/retry` can still use it. The one exception is
withdrawal, where destroying it is the point.

## 2. Being offline is not a failure

`OfflineError` does **not** increment the attempt counter or escalate the
backoff; it just re-schedules ~10s out. Counting an outage toward
`MAX_ATTEMPTS` would dead-letter perfectly good recordings during exactly the
event this queue exists to survive — a clinic wifi drop. Only real
rejections (a 4xx/5xx, a corrupt session) consume the budget.

`MAX_ATTEMPTS` is a real ceiling (8) rather than infinite, because retrying a
permanently broken upload forever *is* the silent failure P0-2 forbids. At
the ceiling the entry surfaces to the doctor with its last error and a Retry
button.

Backoff is jittered. After a clinic-wide outage, several laptops retrying on
identical schedules would hit the API in synchronised waves.

## 3. Recorder chunks and S3 parts are different granularities, deliberately

Restating decision 0026 §3 because this is where it gets used: chunks are
~5s/~20 KB so a crash costs one chunk; S3 parts are ≥5 MB except the last,
which at mono Opus 32 kbps is ~21 minutes of audio. `planParts` assembles
many chunks into each part. A typical consult is a single part — legal
because S3 exempts only the *final* part from the minimum.

Getting this backwards (one part per chunk) is rejected by S3 for every part
but the last, and only against a real bucket — which is why `planParts` is
pure and unit-tested, and why the end-to-end test runs against real MinIO
rather than a mock.

## 4. Two bugs the tests' own output exposed, both about trust in the status readout

Neither broke the upload. Both would have quietly undermined the one thing
P0-2 asks the UI to do.

**"Recording was interrupted" on a normal stop.** `recoverInterrupted()` ran
on every queue tick and promoted any entry in state `recording` to `pending`.
It could not distinguish *the app crashed mid-recording* from *recording is
happening right now in this tab* — so a normally-stopped 14-second recording
was labelled interrupted, and the upload was queued **while capture was still
running**, risking chunks written after the upload being deleted unsent.

Fixed with a heartbeat: the recording screen touches the entry every 5s, and
`recoverInterrupted` only claims entries stale by more than 30s. A timestamp
rather than an in-memory flag, because it has to survive the process dying —
which is the case being detected.

**Progress over 100%: "56 KB of 37 KB".** `bytesTotal` was passed in from
React state captured *before* `stop()` flushed MediaRecorder's final chunk,
so it was short by exactly one chunk. `markReadyToUpload` now derives the
total from the chunk store, which is the only thing that knows what is
actually on disk. Both now have regression assertions, because both passed
the original test suite while being visibly wrong in its own output.

## 5. Device-full is checked before recording, in minutes not percentages

An IndexedDB write failing with `QuotaExceededError` halfway through a
consultation loses the rest of it, and there is no graceful recovery in the
moment. So `checkStorage()` runs before capture starts and blocks at
`critical`.

The threshold is expressed as **minutes of recording remaining**, not a
percentage: "8% free" means nothing to a doctor, while "about 20 minutes
left" is directly comparable to the length of a consultation. Note the
feedback loop worth surfacing — space frees up *because* the queue drains, so
a shrinking disk usually means uploads are stuck, and the queue panel is
where that is visible.

## What would change my mind

- **On waiting for `transcribed`:** if real ASR latency makes that wait long
  enough that laptops fill up, the answer is a dedicated server-side
  "durably stored" acknowledgement — S3 has the object *and* the transcript
  job is committed — rather than relaxing back to deleting on `complete`.
- **On the 30s staleness window:** if a laptop is ever slow enough that a
  live recording misses two heartbeats, it would be wrongly recovered
  mid-capture. A per-tab lock (Web Locks API) would be strictly better than a
  timeout; the timeout is the version that also survives a crash, so it stays
  as the floor either way.
- **On per-encounter keys:** `enqueueRecording` currently derives the
  idempotency key from the encounter id. That is stable and sufficient today
  because consent (and therefore the encounter) must exist before recording
  can start. If offline recording of a *new* encounter is ever allowed —
  which 2.3's fail-closed consent gate presently forbids — the key must be
  generated locally first and the encounter created later against it.
