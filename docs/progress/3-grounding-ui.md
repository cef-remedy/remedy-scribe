# Phase 3 — Grounding UI (P0-7)

**Status:** done · **Date:** 2026-08-27
**Related decisions:** [0030](../decisions/0030-grounding-is-withheld-not-approximated.md)
(why grounding is withheld rather than approximated),
[0013](../decisions/0013-presigned-multipart-upload-design.md) (presigned
object access), [0016](../decisions/0016-transcript-storage-shape.md) (one
source of truth for timings),
[0027](../decisions/0027-consent-flow-ordering-and-withdrawal.md) (why
revision ordering is not leaned on)

The doctor can now ask any line of a drafted note where it came from, see the
transcript passage it cites, and hear that moment of the consultation. This is
the feature the last four phases were building toward: segment IDs assigned at
persist time (1.2), the model citing those IDs instead of inventing offsets
(1.4), citations verified rather than trusted (1.4), and a note that stores
sections separately so each can be addressed (0.x). If any of those had cut a
corner, it would show here.

## The rule that drove every decision

**For a feature whose only job is proof, a confidently wrong answer is worse
than no answer.**

An empty panel tells a doctor "check this yourself." A panel highlighting the
*wrong* sentence tells them "this was verified" — and they sign on that. The
second failure is silent, and it is where every obvious implementation ends
up. Three of them:

### Stored offsets stop being true the moment a doctor edits

`source_spans` holds character offsets into the section text *as generated*.
P0-5 requires free editing before signing. An insertion in sentence one
shifts every later offset — and slicing by stale offsets still *works*, it
just returns the wrong substring.

`spans_fit_text` re-derives the invariant from the data instead of trusting
it: generation joins per-sentence strings with a single space, so slicing the
current text by the stored spans and re-joining with a space must reproduce
the current text exactly. When it doesn't, the section renders as plain text
and says *why*. No extra storage, no migration.

A **same-length** rewrite is the subtle case: the offsets stay structurally
valid (they do still delimit that sentence) but the words are now the
doctor's. So `edited_since_generation` is a second, separate flag — two
questions, two answers.

### The database's belief about audio is not evidence

The bucket's own lifecycle rule expires recordings after
`audio_retention_days`, and **nothing writes back to the encounter row**. So
`audio_object_key` set with `audio_deleted_at` NULL does not mean the bytes
exist. Trusting the row is precisely how a doctor gets the dead play button
this phase's heads-up warns about.

`_audio_state` asks storage with a `HEAD` before any play button is offered,
and when the object is gone while the row claimed otherwise it stamps
`audio_deleted_at` — the lifecycle rule is the only thing that could have
removed it, so that is fact, not inference, and the `HEAD` is paid once
instead of on every note open.

Storage being unreachable is its own state. "We could not check" and "it is
gone" are different facts, one of them permanent.

### The transcript is not shipped wholesale to render a highlight

Only the cited segments plus one neighbour either side are returned. The
transcript is the most sensitive artifact in the system — verbatim, including
what the doctor chose not to write down — and a 30-minute consult is hundreds
of segments. Neighbours come along flagged `cited: false` and ranked visually
below the cited ones, because context helps but a neighbour is not evidence.

## What was built

**Backend**

- `app/services/grounding.py` — `resolve_grounding` (spans → passages →
  timestamps, with both validity flags), `spans_fit_text`, `_audio_state`'s
  five-state ladder, `presign_playback_url`.
- `GET /notes/{id}/grounding` — one read for the whole view. Audited as
  `note.grounding.read`, **separately from `note.read`**: reading a note
  returns the clinician-facing summary, this returns verbatim transcript
  passages, and a strictly larger disclosure deserves its own accountable
  action.
- `GET /encounters/{id}/audio-url` — a presigned GET minted **on demand**.
  409 (not 404) when the audio is gone, carrying the reason. Audited as
  `encounter.audio.playback_url`, without the object key.
- `storage.presign_audio_playback` — signs `Cache-Control: no-store` and
  `Content-Disposition: inline` into the URL, so the bytes stay out of the
  browser cache and out of the Downloads folder.
- `s3_playback_url_expires_seconds` (300s) as its own setting, shorter than
  the 900s part-upload window but not so short that a Range-streamed passage
  breaks mid-playback.

**Frontend**

- `lib/grounding.ts` — the two rules enforced in one place:
  `groundableLines` returns nothing rather than something plausible when
  `spans_fit` is false; `audioNotice` turns each rung of the ladder into words.
- `lib/usePassagePlayer.ts` — plays a **window**, not a file. Stops at the
  passage's end plus a 250 ms tail (`end_ms` is the ASR's last-word timestamp
  and `timeupdate` ticks at ~250 ms, so cutting exactly clips the final word).
  Releases the stream on unmount; nothing touches IndexedDB.
- `components/GroundedSection.tsx` — sections render as clickable lines first,
  textarea on explicit "Edit this section". Two taps: highlight, then audio.
- A line citing **nothing** gets a wavy underline and is called out in words.
  It is the line most worth a second look; rendering it as ordinary prose
  would hide the one signal grounding exists to produce.

## How this changed 2.6

Grounding needed the note to be clickable prose, and 2.6 had made every
section a textarea. Sections now default to lines and swap to a textarea on
request. That ordering is deliberate — the first pass over an AI draft should
be verification, the same reason P0-4 specifies APSO — but it *is* a change to
an affordance that already existed, so the 2.6 smoke test's assertions were
rewritten against the new DOM rather than left to rot.

One of them got stronger: `editing is disabled once signed` became **a signed
note offers no editable field at all**, plus a new check that a signed note
can still be checked against its sources. Absence is a better guarantee than a
disabled attribute.

## Verification

**API: 206 passing** (up from 173 — 33 new in `test_grounding.py`), `ruff`
and `mypy` clean.

**Web: 60 unit tests** (up from 42 — 18 new in `lib/grounding.test.ts`),
`tsc` clean on both projects.

**End-to-end: `smoke/grounding-flow.cjs`, 39/39** against real Postgres,
Redis, MinIO and a real Chromium, with a real 14-second recording uploaded to
real object storage:

| | |
|---|---|
| grounding loads for a freshly generated note | pass |
| cited passages carry real audio timings, not nulls | pass |
| a bounded slice of the transcript is returned, not all of it | pass |
| note lines render as clickable evidence, not a textarea | pass |
| the affordance is explained rather than left to be discovered | pass |
| tapping a line reveals its source transcript | pass |
| each passage shows a speaker and a timestamp | pass |
| neighbours are labelled context, not evidence | pass |
| **no audio starts on the first tap** | pass |
| audio playback starts on the second tap | pass |
| the sounding passage is marked in the panel | pass |
| **playback stops at the end of the cited passage, not the recording** | pass |
| the URL is signed no-store | pass |
| the URL is short-lived (300s) | pass |
| **the recording answers Range requests (HTTP 206)** | pass |
| an edit is reported as no longer fitting | pass |
| the edit is recorded, so passages are not claimed as the doctor's words | pass |
| the screen says why grounding is withheld | pass |
| withdrawal is reported as withdrawal | pass |
| the transcript still grounds after the audio is gone | pass |
| the refusal says "deleted at the patient's request" | pass |
| the screen explains the state rather than showing a dead play button | pass |

**Regression:** `note-flow.cjs` **32/32** (up from 30), `consent-flow.cjs`
35/35, `record-flow.cjs` 22/22, `auth-flow.cjs` 17/17.

### What could not be run here, stated plainly

`GROQ_API_KEY` and `ANTHROPIC_API_KEY` are not provisioned in this
environment, so the ASR and note-generation legs could not run. Rather than
skip the end-to-end verification, `smoke/seed_pipeline.py` substitutes those
two calls behind an explicit `SEED_PIPELINE=1` flag: the recording, the upload,
the object in MinIO, the presigned playback, the Range requests, the span
resolution and the entire browser UI are all real — only *how the draft came
to exist* is stood in for, and that path is verified for real in Phase 1.3 and
1.4. Run without the flag to exercise the true chain.

Consequently `upload-queue.cjs` reports **3 of 18 failing** — all three are
"pipeline_status is still `uploaded`", which is the *correct* behaviour when
transcription cannot run, and P0-2's rule that local audio survives until the
pipeline confirms is working as intended. The worker log shows 29 occurrences
of `GROQ_API_KEY is not set` and no other error class.

## Notable bugs caught

- **The playback assertion was checking too late, not failing.** The first run
  reported "audio playback starts on the second tap: FAIL". Playback was in
  fact working — the cited passage is 1,740 ms long, and by the time the
  assertion ran 2,500 ms later it had already **stopped at `end_ms`**, exactly
  as designed. Diagnosed by driving `new Audio()` against the presigned URL
  directly in the page and reading back `readyState`, `currentTime` and the
  event log. Fixed by polling for the playing state within the passage's own
  length, and then asserting the far more valuable property: that playback
  *stops* and does not run on through the consultation.
- **`--card2` was referenced but never defined.** `.prior` (the prior-visit
  card, added in 2.6) has had no background since it shipped, because the
  custom property it uses was never declared in `:root`. Found while adding a
  second consumer. A CSS `var()` with no fallback and no definition fails
  silently — nothing in `tsc`, the build, or any test sees it.
- **Three smoke helpers pinned IndexedDB to a version the app had left
  behind.** `indexedDB.open("remedy-scribe", 1)` throws `VersionError` once the
  store is at v2 — which it has been since Phase 2.2. `consent-flow.cjs` and
  `record-flow.cjs` were therefore *unrunnable*, failing with a driver error
  partway through rather than a check failure, which is why it went unnoticed.
  Fixed by dropping the version argument: a read-only probe has no business
  dictating the schema.

## Open follow-ups

- **Grounding validity is per-section, not per-span.** A typo fix in one
  sentence withdraws grounding for the whole section. That is the safe
  direction to be wrong in, and it is the granularity generation writes, but
  Phase 6's edit-burden data will show whether small fixes are common enough
  to justify span-level validity.
- **Audio objects are stored with a generic `.audio` extension.**
  `_CONTENT_TYPE_EXTENSIONS` does not handle a codec parameter, so
  `audio/webm;codecs=opus` misses the lookup. Harmless — the S3 `ContentType`
  is stored correctly and playback works — but a human browsing the bucket
  cannot tell what the files are.
- **`duration` is `Infinity` for these recordings.** MediaRecorder writes a
  WebM header with no duration. Chromium seeks correctly regardless (verified),
  but it means no total-length UI is possible without remuxing, and other
  browsers should be checked before the pilot.
- **Retention still deletes nothing.** Both `retention_expires_at` columns are
  written and read by no job (Phase 4.4). The `expired` audio state is
  therefore reachable today only via the bucket lifecycle rule, which is
  exactly the case the `HEAD` check exists to catch.
- **The review screen still shows a patient id, not a name** — carried over
  from 2.6; `GET /patients/{id}` does not exist.
