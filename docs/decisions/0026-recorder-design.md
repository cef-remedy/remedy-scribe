# 0026 — Recorder design: fail-closed consent, recorded gaps, crypto-shred

**Phase:** 2.2 · **Decided by:** implementation · **Date:** 2026-08-27

Four decisions fell out of building the recorder. None was a 🧠 in the
checklist, but each is the kind of choice that is invisible once made and
expensive to reverse.

## 1. The consent gate reads from the server and fails closed — including offline

P0-1 requires the app to block recording "**before anything is captured**".
The server already refuses to finalize an upload or transcribe without
consent (Phase 0.1), but both happen *after* capture, so neither satisfies
that clause. The client needs its own gate.

Two sub-decisions:

**The gate reads the ledger from the server, not local state.** A reload
mid-encounter loses local state while the ledger entry persists, so local
state fails *open* — the one direction a consent gate must never fail. This
required a new endpoint, `GET /api/v1/consent/{encounter_id}`, built on the
same fold `assert_consent_valid` uses (`current_consent_state`) so the read
and the enforcement can never disagree. A test asserts that agreement
directly across five ledger sequences rather than trusting the shared
helper.

**Offline blocks recording.** This is a real UX cost, stated plainly: a
doctor with no connection cannot start a *new* consented recording. The
alternative — assuming consent because we cannot check — is unlawful
recording under RA 4200, which is not a trade this gate gets to make. Phase
2.3 can soften it by caching a *positive* ledger read for the current
encounter, which is safe precisely because consent demonstrably existed at
read time.

**The gate is re-checked at the moment of the tap**, not only on mount.
Consent can be withdrawn while the recording screen sits open, and a
mount-time answer would be stale. Cheap, and it is the difference between a
gate and a hint.

## 2. Gaps are detected and recorded, not hoped against

Decision 0024 established that lid close / system sleep loses audio and that
no client architecture can prevent it — it is OS power policy. The harness
run lost 6.5 seconds that way.

Given that, the choice is not *whether* audio can be lost but what happens
when it is. Silently presenting truncated audio as complete is the failure
the PRD explicitly rejects ("not a silent gap in the record"). So the
recorder:

- Runs an **AudioWorklet counting samples** as ground truth. The count only
  falls behind if the audio graph actually stalled — codec-independent, and
  unlike byte counting it is not fooled by Opus encoding silence to almost
  nothing (a quiet consult room would otherwise look like data loss).
- Detects **suspends** by watching for a wall-clock jump far larger than its
  own monitor interval, and **stalls** by watching for worklet silence.
- Surfaces missing time **in the recording indicator itself**, not buried in
  a details panel, and shows a plain-language explanation naming the likely
  cause ("the laptop went to sleep — most likely the lid was closed").

Anchoring matters and was already a bug once: the measurement starts at the
first worklet message, not at the button press. The gap between them is
startup latency (~0.7–1.3s measured), and charging it to "missing audio"
produced a false loss warning on a healthy run in the harness.

**Carry-forward:** these gaps are currently reported to the doctor only.
They should eventually reach the note generator, which already suppresses
content over low-confidence windows (Phase 1.4) — a known audio gap is
strictly better evidence than low ASR confidence and deserves the same
treatment. Flagged in the Phase 2.2 writeup, not built here.

## 3. Recorder chunks are ~5s, deliberately unrelated to S3's 5 MB parts

Two granularities that are easy to conflate:

| | size | why |
|---|---|---|
| Recorder chunk | ~5s, ~20 KB | a crash or suspend costs at most one chunk |
| S3 upload part | ≥5 MB (except the last) | `MIN_PART_SIZE_BYTES`, Phase 1.1 |

At mono Opus 32 kbps, **5 MB is about 21 minutes of audio**. Sizing recorder
chunks to the S3 minimum would mean risking 21 minutes per crash — the exact
opposite of what a write-ahead log is for. Phase 2.4 assembles many chunks
into each part; they are emphatically not 1:1.

`assembleSession()` exists for that upload path and is the one place a whole
consult is held in memory. At 32 kbps a 30-minute consult is ~7 MB, which is
acceptable in a way the harness's accidental 129 kbps (~27 MB) would not have
been — worth remembering if the bitrate ever rises.

## 4. Crypto-shred is the withdrawal primitive, and its blast radius is per-device

Audio is AES-GCM encrypted with a **non-extractable** `CryptoKey` held in
IndexedDB. `extractable: false` means the raw bytes cannot be read out at
all — not by this code, not by an XSS payload — which is the difference
between "encrypted at rest" as a checkbox and as a property. GCM's
authentication also means a tampered chunk *fails* to decrypt rather than
decrypting into garbage that then gets transcribed as if it were speech.

`destroyAudioKey()` renders every stored chunk permanently unreadable, which
is a stronger deletion primitive than removing rows: it cannot leave a
recoverable fragment. But the key is **per-device, not per-encounter**, so
it shreds *all* locally-queued audio. Correct for "wipe this laptop", wrong
as a per-encounter withdrawal — for that, `deleteSession()` removes one
encounter's chunks and the server-side path handles anything uploaded. Both
exist; using the wrong one would destroy other patients' pending recordings.

**The honest limitation, restated because the checklist demands it:** a
browser cannot seal this key in hardware the way iOS Keychain or Android
Keystore could. `extractable: false` is enforced by the browser, not a
secure element. If Legal requires hardware-sealed key custody for on-device
PHI, this is the item that forces decision 0024's Electron option
(`safeStorage` → DPAPI/Keychain).

## What would change my mind

- **On the offline gate:** if week-0 shadowing shows clinic wifi drops
  often enough that fail-closed blocks real consultations, the fix is 2.3's
  cached positive read — not relaxing the gate. If that is still not enough,
  it becomes a question for Legal about whether a locally-captured consent
  signature can stand in for a ledger read, which is their call and not
  engineering's.
- **On per-device keys:** a per-encounter key would make crypto-shred a
  precise withdrawal primitive and remove the footgun above. It costs a key
  per encounter to manage and would need the encounter id available before
  the first chunk — worth doing if withdrawal-during-recording turns out to
  be common in practice.
