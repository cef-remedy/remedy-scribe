<!-- artifact: https://claude.ai/code/artifact/e0d038a9-f414-4b93-bc0a-87391d3b78cf (docs/implementation-checklist.html) -->
# Remedy Scribe — Production Implementation Checklist

**Purpose:** everything between the current scaffold and a system that can legally and safely record real consultations in a Remedy clinic.
**Companion docs:** `remedy-scribe-prd.md` (what/why) · `remedy-scribe-roadmap.md` (when) · `docs/tech-stack.md` (with what)

---

## Refresh log — 2026-08-27 (Phase 2.4: offline upload queue — the loop closes)

**Progress: 61/120, up from 55.** Audio recorded on a laptop now reaches S3, the Phase 1 pipeline runs on it for real, and the local copy is deleted only once that has happened. Until this phase, everything built server-side in Phase 1 had no real input.

**The write-ahead invariant, asserted rather than described.** The queue entry — carrying the idempotency key — is written when recording *starts*. The end-to-end test checks exactly that: at t+0.9s the entry exists in state `recording` with its key persisted and **zero chunks on disk**. That ordering is the point of the phase: a key generated in memory and lost to a crash produces a second key on retry, which is the duplicate-encounter bug the key exists to prevent.

**"Receipt" and "pipeline start" are different events, and only one of them is a 200.** `upload/complete` confirms S3 holds the object and a Celery chain was *enqueued* — nothing about whether a worker ran. A broker outage leaves `pipeline_status` at `uploaded` forever, so deleting on the 200 would destroy the only copy of a consultation whose processing never began. The queue therefore has a distinct `uploaded → confirmed` step polling a new `GET /encounters/{id}`, advancing only at `transcribed`. The mirror case matters too: a *terminal* server failure keeps the local audio, because it may be the only copy.

**Offline is not a failure.** `OfflineError` neither increments the attempt counter nor escalates the backoff — counting an outage toward the retry ceiling would dead-letter healthy recordings during exactly the event this queue exists to survive. Backoff is jittered so several laptops recovering from one wifi outage do not hit the API in synchronised waves.

**Two bugs found by reading the end-to-end test's own output, not by its assertions.** Both left the upload working while making the status readout lie — and the status readout *is* the P0-2 requirement. (1) A normally-stopped 14-second recording was labelled *"Recording was interrupted"* and queued for upload **while still capturing**, because the recovery pass could not tell a crashed recording from a live one; fixed with a heartbeat plus a staleness window. (2) The panel showed *"56 KB of 37 KB"* — progress over 100% — because the byte total came from React state captured before `stop()` flushed the final chunk; now derived from the chunk store. Both have regression assertions.

**Verified against the real stack**, not mocks: Postgres, Redis, MinIO, a Celery worker, and a real Chromium. **18/18** end-to-end including a genuine 48,774-byte presigned upload to MinIO, `pipeline_status=note_generated`, and local chunks going 3 → 0 only after that. API **146 passing** (up from 143, including a route-order regression guard — a path parameter declared before `/loose` would silently swallow the worklist). Web **42 unit tests** (up from 18), `tsc` clean, build fine.

---

## Refresh log — 2026-08-27 (Phase 2.3: consent flow — the legal gate)

**Progress: 55/120, up from 49.** The consent flow is complete as a mechanism: bilingual script, participant roster, decline, mid-visit re-consent pause, and withdrawal that actually deletes. **It cannot ship to patients yet, and not for an engineering reason** — the script text is a placeholder written by an engineer, and RA 4200 clearance by Philippine counsel is the PRD's own *blocking* open question. The app displays that caveat on screen, and the text is isolated in one file so counsel's version is a single edit.

**P0-1's first two bullets constrain each other, and reading either alone gets it wrong.** Bullet 1: the script is presented "before anything is captured". Bullet 2: once consent is given, "the spoken exchange is captured as the first segment". The tempting reading — start recording, read the script, and the recorded asking becomes segment 1 — satisfies bullet 2 and **violates bullet 1**. The only sequence satisfying both is roster → script → log the outcome → start recording → speak a short confirmation. So the consent screen never touches the microphone, and a smoke check asserts it. The consequence Legal needs told explicitly: the patient's own spoken "yes" is *not* on the recording, only the doctor's confirmation that it was given.

**Decision 0003 is closed after being open since Phase 0.1 — by elimination, not by choice.** It offered manual flagging vs. ASR-diarization detection of a new speaker; decision 0018 removed diarization entirely, so there are no speaker labels for the second option to read. Manual flagging is the only implementable option, and the original concern (a doctor mid-exam simply forgetting) is unaddressed — what the design does instead is make remembering cheap and the state honest: the pause happens before any network call, and resuming is gated on the ledger write rather than the doctor's word.

**Withdrawal now has real server-side effects**, closing the checklist's own heads-up. Ledger entry committed first (the legal record), retention clock set to now (durable backstop), then a best-effort immediate object delete. No attempt is made to kill a running Celery task, and the UI says "next stage boundary, not instantly" — asserted by a smoke check, because that sentence is also what Legal will be told.

**Pausing turned out to collide with 2.2's gap detection three ways**, each producing a confidently wrong reading: the worklet counting samples the recorder no longer writes (pause reported as lost audio), the stall detector firing on expected silence, and the pause duration reading as a system suspend. Plus one that silently eats audio: a paused `MediaRecorder` ignores `requestData()`, so `stop()` must resume before flushing or the tail is discarded.

**Verified:** API **143 passing** (up from 136), `ruff`/`mypy` clean. Web 18 unit tests, `tsc` clean. And **35/35 end-to-end in a real Chromium** against the live API, with every check mapped to a P0-1 clause — including that the microphone is never touched on the consent screen, that paused time is reported separately rather than as missing audio, and that withdrawal takes local chunks from 3 to 0.

---

## Refresh log — 2026-08-27 (Phase 2.2: recording)

**Progress: 49/120, up from 43.** The app records for real: mono Opus at 32 kbps, AES-GCM encrypted before anything touches disk, written in ~5s chunks to IndexedDB so a crash or a lid-close costs at most one chunk. Everything load-bearing came from the capture harness rather than assumption — the wake lock re-acquired on every return to visible, an explicit `audioBitsPerSecond`, and a requested constraint never trusted as an achieved setting.

**The P0-1 consent gate is real and it is the only path to the record button.** P0-1 requires blocking *"before anything is captured"*, and the existing server enforcement (upload confirmation, transcription) both run after capture — so this needed a new read, `GET /api/v1/consent/{encounter_id}`, built on the same ledger fold `assert_consent_valid` uses. It fails closed on every uncertain path including offline, which is a real UX cost stated plainly in decision 0026: assuming consent because we cannot check it would be unlawful recording under RA 4200. Until 2.3's bilingual flow exists a doctor genuinely cannot record — the correct state for a system whose legal basis is not yet implemented, rather than a temporarily-open path with a TODO.

**Audio gaps are now recorded rather than hidden.** Decision 0024 established that lid close loses audio and no client architecture can prevent it. Given that, the only honest design is to detect it: an AudioWorklet counts samples as ground truth, wall-clock jumps read as suspends, worklet silence as stalls, and missing time appears **in the recording indicator itself** with a plain-language cause.

**Verified:** API **136 passing** (up from 131), `ruff`/`mypy` clean. Web **18 unit tests** (vitest + a real `fake-indexeddb`, not a stub) covering what fails silently — key non-extractability, plaintext never reaching disk, cross-session leakage, IV freshness, GCM rejecting tampered chunks, reassembly order. And **22/22 end-to-end in a real Chromium** against the live API: gate blocks with no consent, recording runs once consent exists, 4 chunks / 65 KB land in IndexedDB with the WebM magic bytes provably absent, 18s elapsed against 17s captured and **0:00 missing**, withdrawal re-blocks.

**Decision 0025 confirmed in practice:** 65,218 bytes for 18 seconds is **~29 kbps**, against the 32 kbps target and the harness's accidental 129 kbps.

**One real bug found by a test hook timing out:** `getAudioKey()` leaked an IndexedDB connection. The production consequence is worse than the test symptom — leaked connections accumulate one per recording across a clinic day, and an open connection blocks `onupgradeneeded`, so a future `DB_VERSION` bump would hang for anyone with the tab open.

---

## Refresh log — 2026-08-27 (Phase 2.1: web app foundation, client re-platformed)

**Progress: 43/120, up from 37.** The client is now a **browser web app on a clinic laptop**, not an Expo mobile app (decision 0024). This was not a preference: the supervisor answered the PRD's open question *"what devices do doctors actually carry"* — laptops — and both reasons `tech-stack.md` §1 gave for rejecting a web client were specific to a phone. `apps/mobile/` is deleted (git history retains it); `apps/web/` replaces it.

**Measured before committing to it.** `docs/experiments/audio-capture-harness.html` ran 29 minutes on the real hardware: audio lost during 131s of backgrounding across 9 windows was **0.05s**, page timers did not throttle at all, and every measurable loss (6.5s of 7.7s) came from one system suspend. Lid close is the single real gap and it favours no architecture — it is OS power policy that neither a browser nor Electron can veto. Full accounting in decision 0024.

**Two backend changes a browser needs that a native client never did, both of which fail *silently*:** CORS (without it the preflight is rejected and the request never reaches a route, so nothing appears in the API log at all) and an **httpOnly refresh cookie**. The cookie is the one place the browser is strictly *stronger* than the retired mobile plan: JavaScript cannot read it, which `expo-secure-store` could never promise. Decision 0006 is amended, not reversed — the access token stays short-lived and in memory.

**Re-verified after implementing:** full API suite — **131 passing** (up from 121; 10 new in `tests/test_web_client_support.py`), `ruff` and `mypy` clean, postgres and MinIO testcontainer tests included. Web app: `tsc` clean, production build succeeds (79 KB gzipped, service worker precaching 8 entries). And a **17-check end-to-end smoke test through a real browser against the live API** (`apps/web/smoke/auth-flow.cjs`) — login with a live TOTP code, cookie asserted httpOnly and path-scoped, no token in any JS-readable storage, session restored from the cookie alone after a full reload, logout leaving no zombie session.

**Three real bugs found by running it, not by reading:**
1. **Refresh-token precedence was backwards.** Preferring the cookie over an explicitly-presented body token silently broke Phase 0.3's reuse detection — a caller naming a deliberately-stale token got the valid cookie rotated instead and received 200 where 401 was required. Worse, single-session logout would have revoked whichever session the cookie happened to hold rather than the one named. Body-first now, asserted directly.
2. **Cookie-clearing on the error path never reached the browser.** Mutating the injected `response` and then raising `HTTPException` discards the mutation — FastAPI builds a fresh response for the exception. A dead cookie left in place guarantees every future silent renewal fails identically instead of falling through to a real login. Now carried on the exception itself.
3. **A 422 rendered as "Could not sign in. Please try again."** — indistinguishable from wrong credentials, sending a doctor to re-check their password when the real problem is the email. Reachable by a real user, not just a buggy client: `a@b.test` passes the browser's own `type="email"` validation but the API rejects RFC 2606 reserved TLDs via pydantic `EmailStr`.

**One item deliberately deferred rather than silently dropped:** biometric unlock. The original item assumed a personal phone. On a *shared* clinic laptop, WebAuthn (Windows Hello / Touch ID) authenticates the machine's logged-in user, so if doctors share one Windows session a biometric prompt proves nothing about which doctor is signing — arguably worse than nothing, because it looks like proof. Needs an answer to "do doctors share a Windows login?" first. See the 🧠 in 2.1.

---

## Refresh log — 2026-08-25 (Phase 1.5: pipeline failure handling — Phase 1 now fully closed)

**Progress: 37/120, up from 33.** Two new terminal `EncounterPipelineStatus` members (`transcription_failed`, `generation_failed` — deliberately not a third `upload_failed`, decision 0023) back a real dead-letter path: `_mark_stage_failure` in `app/tasks/pipeline.py` records `retry_count`/`last_pipeline_error` on every failed attempt and flips to the terminal status once retries are exhausted, reset to a clean slate on the next success. `GET /encounters/failed` surfaces the dead letter (no app exists yet to surface it in); `POST /encounters/{id}/retry` re-runs only the stage that failed, not the whole pipeline — a `GENERATION_FAILED` retry never re-pays for a real ASR call the first attempt already got right.

**The 📚 "stuck work is the real hard part" note is followed as two separate mechanisms, not one (decision 0023):** dead-lettering only catches a task that ran and raised. `sweep_stuck_encounters`, on a new Celery Beat schedule (every 5 minutes, `infra/docker-compose.yml`'s new `beat` service), catches the other failure mode — a task that never ran at all — by comparing a new `pipeline_updated_at` column against a configurable staleness threshold instead of catching an exception that was never thrown.

**Re-verified after implementing:** full suite — **121 passing** (up from 107; 13 new tests in `tests/test_pipeline_failure_handling.py`, 1 new RBAC regression). `ruff` and `mypy` both clean (55 source files). Migration `c9d0e1f2a3b4` applied cleanly against a real Postgres container as part of the full run.

**One real bug found while writing the tests, not by reading:** the first version of `sweep_stuck_encounters` dispatched via a dict built at module load time (`{UPLOADED: run_pipeline, TRANSCRIBED: run_note_generation}`). That dict captures the two functions' identities once, at import — a test's `monkeypatch.setattr("app.tasks.pipeline.run_pipeline", ...)`, which replaces the module attribute, has no effect on a reference already stored in the dict. The test's fake never ran; the real `run_pipeline` did, which tried to open a real Redis connection and hung the test run. Fixed by dispatching on a plain `if`/`else` referencing the bare names inside the function body, so they resolve from the module's current global namespace at call time — see docs/progress/1.5-pipeline-failure-handling.md for the full account.

**Phase 1 is now fully closed** (1.1 through 1.5).

---

## Refresh log — 2026-08-25 (Phase 1.4: real note generation)

**Progress: 33/120, up from 28.** `HaikuNoteGenerator.generate` is implemented for real — a single fused Anthropic Messages call, forced tool-use for structured output (`tool_choice` pins the model to exactly one tool; no free-text parsing), APSO section order, hedged language required by the system prompt, and two mechanical (not instruction-following) suppression layers: low-confidence words become a literal `[INAUDIBLE]` in the prompt before the model ever sees them, and the schema's `suppressed` field forces empty text server-side regardless of what the model also emitted.

**The 🧠 "how do you get trustworthy source spans?" call is resolved (decision 0022):** segment IDs — reusing the transcript segment `id` already assigned at persistence time (decision 0016), not a new sentence-numbering scheme. The model cites `segment_ids`; any ID that doesn't match a real segment sent in the prompt is dropped, not trusted. Character offsets (`text_start`/`text_end`) are never asked of the model — the server computes them exactly by tracking a cursor while concatenating the model's own sentences.

**Re-verified after implementing:** full suite — **107 passing** (up from 91; 16 new tests in `tests/test_note_generation_haiku.py` covering prompt formatting, tool schema shape, span computation, citation-hallucination dropping, suppression enforcement, the API-key gate, the empty-transcript cost short-circuit, HTTP-error propagation, and a real (un-mocked) httpx request-building check plus a golden-transcript end-to-end case). `ruff` and `mypy` both clean (56 source files). Migration `b8c9d0e1f2a3` (`notes.prompt_version`) applied cleanly on top of the existing 7-migration chain.

**One test-suite fix required by this phase's own change, not a new bug:** `TranscriptSegment` gained an `id` field (Phase 1.4 needs a stable citation target), so three pre-existing round-trip tests in `test_transcript_persistence.py` that compared loaded segments against the `id=None` fixtures needed a `_with_ids()` helper — persistence assigning real IDs is the correct new behavior, the tests were asserting the old one.

---

## Refresh log — 2026-08-25 (note generation: Haiku only, Luna dropped)

**Progress: 28/120, unchanged.** A planning-ahead update to Phase 1.4, not new work done — the user's call (decision 0021): Claude Haiku 4.5 is now the sole note generator; `LunaNoteGenerator` and `app/services/note_generation/luna.py` are deleted, not kept dormant. **This drops the risk mitigation P0-4 named explicitly** — "Haiku remains available as a configured fallback if Luna underperforms" — since there is no longer a second real provider to fall back to. Not a defect; a deliberate trade the checklist item below is annotated with, same treatment as the ASR vendor swap (decision 0018).

**Re-verified after making the change:** full suite — **91 passing** (unchanged from before this edit — Phase 1.4 isn't built yet, so nothing exercised the deleted code path). `ruff` and `mypy` both clean (55 source files now, down from 56 — one file fewer). App boots and `/health` responds.

**A second finding, smaller but real:** the local (uncommitted) `apps/api/.env` still had `NOTE_GENERATOR_PROVIDER=luna` and a stale `ELEVENLABS_API_KEY=` line from before Phase 1.3 — invisible until now because `SettingsConfigDict(extra="ignore")` silently drops unrecognized keys, and `luna` only started failing once the `Literal` type narrowed to `["haiku"]` alone. `.env` drift under `extra="ignore"` is invisible by construction for any field not validated against a closed set — see decision 0021.

---

## Refresh log — 2026-08-25 (mypy baseline, ASR vendor references)

**Progress: 28/120, unchanged from the last run** (this refresh re-verified ground truth and audited existing code; it didn't advance any new checklist item). Full audit trail per subphase lives in `docs/progress/` and `docs/decisions/`.

**Re-verified this run, all fresh (not carried over from memory):** full test suite — **91 passing**, up from the 90 last reported, because this run's audit added one regression test (see below). `ruff check` — clean. `mypy` — **clean, for the first time this project has run it** (56 source files; previously never run — Phase 5.3's "type-check (mypy)" item was still unchecked and there was no config at all). Alembic's 7-migration chain resolves to a single head and applies cleanly end-to-end against a real Postgres container (exercised by `tests/test_postgres_specific.py`, not just read). App boots and `/health` returns `200 {"status":"ok",...}` against a live process.

**Two real, previously-undiscovered bugs found and fixed by this audit, not by reading:**
1. **`GroqWhisperProvider.transcribe` would have crashed on its first real call.** It passed `data` to `httpx.post` as a list of `(key, value)` tuples alongside `files=`; httpx's multipart encoder requires `data` to be a mapping when `files` is also present, and raises `TypeError` immediately. Invisible to the existing test suite because that test mocked `httpx.post` entirely, bypassing httpx's real request-encoding logic — exactly the class of gap a mock hides (gap-audit class 5, "guarantees your tests never construct," generalized past the DB layer). Fixed (`data={"timestamp_granularities[]": [...], ...}`, the dict-with-list-value form httpx actually expects for repeated multipart fields) and given a dedicated regression test that builds a real httpx request (no network) instead of mocking the call away.
2. **`ASRProvider.model_version` couldn't be a `@property` on a subclass** — mypy caught the LSP violation (overriding a writable base-class attribute with a read-only property) on its first run. Fixed by setting it as a plain instance attribute in `GroqWhisperProvider.__init__` instead.

**One confirmed environment-class limitation, not a bug in this codebase:** MinIO (`RELEASE.2022-12-02`, the version pinned in `infra/docker-compose.yml` and used by the test containers) accepts `PutBucketEncryption` and the `AbortIncompleteMultipartUpload` lifecycle action without error, but doesn't actually enforce either — confirmed by inspecting raw API responses directly, not assumed. Both are correct, standard S3 API calls that a real AWS bucket (this system's actual deploy target) accepts and enforces; see decision 0014.

**One requirement-coverage gap surfaced by the reverse-traceability pass (Step 2):** `remedy-scribe-prd.md`'s P0-3 explicitly requires "speaker diarization enabled." Phase 1.3 implemented ASR with Groq-hosted Whisper instead of the PRD's named ElevenLabs Scribe v2 (the user's explicit call) — Whisper has no diarization mechanism at all, so this half of P0-3 currently has no code behind it anywhere in the system, and won't until one of decision 0018's three options is picked. Not a defect in what was built; a real, currently-open gap against a written P0 requirement, flagged here so it isn't lost between now and Phase 1.4.

**Two small, real gap-audit findings, both cheap, both fixed:** `app/models/patient.py` and `app/services/note_generation/haiku.py` each had one unused import (ruff `F401`) — pre-existing, unrelated to any phase's active work, fixed in passing. `mypy.ini` added (didn't exist before) so botocore/celery's missing type stubs don't drown out real findings on the next run; `types-python-jose`/`types-passlib` added to `requirements-dev.txt` to resolve two more for real instead of suppressing them.

---

## How to read this

| Marker | Meaning |
|---|---|
`- [ ]` | A task. Check it off.
🧠 **Your call** | A real fork in the road. I've listed the options and what each costs you. Don't let me (or anyone) pick this for you silently — write your choice and your reason into `docs/decisions/`.
⚠️ **Heads-up** | A trap that is not obvious from reading the code. Most of these cost people days.
📚 **Understand first** | A concept to hold in your head *before* writing the code, or the code will look arbitrary.

**A note on how to use this while learning:** resist doing these top-to-bottom as dictation. For each 🧠, try to predict the tradeoff before reading my summary — then check yourself. The gap between your guess and the answer is the actual learning. For each ⚠️, ask "how would I have found this myself?" — usually the answer is a test, a type, or a log line you didn't have.

---

## Current state (verified, not assumed)

What actually runs today, confirmed by executing it this session — not by reading the README, and not carried over from an earlier run without re-checking:

**Real and tested (146 API tests + 42 web unit tests + 92 end-to-end browser checks; `ruff`, `mypy` and `tsc` all clean):** the data model (clinicians, patients, encounters, consent ledger, notes, revisions, transcripts, refresh tokens, login attempts, audit log); the full 9-migration Alembic chain, applied for real against a live Postgres container, not just read; the consent ledger's append-only Postgres trigger AND all three `CHECK` constraints (`Note.status`, `Encounter.pipeline_status`, `ConsentLedgerEntry.event`) — all exercised by tests that run real SQL against real Postgres, not asserted from the ORM layer. Consent *enforcement* (server-side, at both `upload/complete` and the head of `transcribe_encounter`). RBAC enforcement (`require_role` attached to every clinical-write route). Refresh-token rotation with reuse detection, login rate limiting/lockout, two-step MFA enrollment. The full presigned-multipart upload flow (`init`/`parts`/`complete`), idempotent end to end, tested against real MinIO via testcontainers — not mocked — including a real presigned-URL PUT round trip. Encrypted transcript persistence, wired into both ends of the Celery chain, each segment carrying a stable citation ID assigned at persist time (Phase 1.4). Real ASR integration (Groq-hosted Whisper large-v3, replacing the PRD's named ElevenLabs Scribe v2 — see the refresh log above and decision 0018) with a real (though never-run-against-a-live-key) HTTP call, turn-order-preserving parsing, and a regression test that builds a real httpx request rather than mocking the call away. Real note generation (`HaikuNoteGenerator`, Phase 1.4) — a single fused, structured-output call producing APSO sections with mechanically-enforced suppression and segment-ID citations verified (not trusted) before persistence; also never run against a live key, but exercised by a golden-transcript test and a real-request-building test the same way the ASR integration is. Pipeline failure handling (Phase 1.5) — dead-lettering into two real terminal statuses with a doctor-triggered `/retry`, plus a separate Celery Beat sweep for encounters that got stuck without ever raising an exception. **A browser client foundation (Phase 2.1)** — `apps/web/`, Vite + React + React Router, its API client generated from the live OpenAPI schema rather than hand-written, and an auth flow driven end-to-end through a real browser against the live API by a 17-check smoke test: login with a live TOTP code, an httpOnly path-scoped refresh cookie, no token in any JS-readable storage, and session resume from the cookie alone after a full page reload. **Real audio recording (Phase 2.2)** — gated behind a fail-closed P0-1 consent read, mono Opus at 32 kbps encrypted with a non-extractable key before it touches disk, chunked to IndexedDB, with audio gaps detected and surfaced rather than hidden; proven in a real browser including an assertion that plaintext audio never reaches storage. **The full consent flow (Phase 2.3)** — bilingual script, participant roster, decline, mid-visit re-consent pause, and withdrawal that deletes local audio and the uploaded object; 35 end-to-end checks, each mapped to a P0-1 clause. Its *mechanism* is complete; its script text still awaits counsel. **A durable offline upload queue (Phase 2.4)** — write-ahead entries in IndexedDB, presigned multipart upload to real object storage, jittered exponential backoff that does not punish being offline, a device-full guard measured in minutes of recording, and local audio deleted only once the server's pipeline has actually run. This is the phase that closes the loop: everything Phase 1 built server-side now receives real input from a real laptop. A live server driven end-to-end with curl through login → patient match → encounter → consent; `/health` returns 200 from a freshly booted process this session.

**Wired but hollow:** `Transcript.retention_expires_at` and `Encounter.audio_retention_expires_at` are both written on every relevant row and read by nothing (Phase 4.4 owns turning that into a policy — see its updated wording below).

**Absent entirely:** the grounding UI's data path (Phase 3); retention *enforcement* (the columns exist, no job reads them); patient identity (2.5), so a recording cannot yet be attached to a named patient; note review, editing and signing (2.6), so a generated note cannot be corrected or signed; and — a genuine, currently-open gap against a written requirement, not an oversight — speaker diarization (P0-3), which the ASR vendor in use structurally cannot provide.

The honest headline: **the full loop now runs — consent, record, upload, transcribe, generate — and what remains is the doctor's half of the workflow.** A recording made on a clinic laptop reaches S3, the Phase 1 pipeline processes it, and the local copy is deleted only once that is confirmed. Verified end to end against Postgres, Redis, MinIO and a Celery worker, not mocks.

What a doctor still cannot do: **attach a recording to a named patient (2.5)**, or **read, correct and sign the generated note (2.6)** — which is the entire point of the product, since P0-5 makes the doctor the accountable signer. And **Legal must still clear the RA 4200 consent script** before any of this touches a real patient; that is the PRD's own blocking open question and not something engineering can close. Phase 0 and all of Phase 1 are closed; Phase 2 is 4 of 6 subphases in.

---

## Phase 0 — Close the holes in what already exists

Do this first. These are not new features; they are places where the scaffold currently *claims* more than it enforces. Shipping features on top of them means the claims stay false.

### 0.1 Enforce the consent gate server-side ⚠️ 🧠

- [x] Add a service function `assert_consent_valid(db, encounter_id)` that checks the ledger for a `given` event with no later `withdrawn` event for that encounter.
- [x] Call it in `confirm_upload` before setting `audio_object_key`, and again at the head of `transcribe_encounter`.
- [x] Return `409` (not `403`) when absent — this is a state problem, not a permissions problem.
- [x] Test: an encounter with no consent row must not be able to reach the pipeline.

⚠️ **Heads-up:** right now nothing server-side stops an encounter from being uploaded and transcribed with **zero** consent records. P0-1 says recording is blocked without consent, and today that rule lives only in the client — which is exactly where a legal control must *not* live, because the client is the part an attacker or a bug controls. This is the single most important gap in the current codebase.

📚 **Understand first:** the difference between a *UX guard* and an *enforcement point*. A greyed-out button is a UX guard. A server-side check that rejects the request is an enforcement point. Compliance controls need the second kind; the first kind is a courtesy. Every P0 requirement in the PRD that says "the app blocks X" should map to a specific server-side rejection you can point at in code.

🧠 **Your call — how strict is the re-consent rule?** P0-1 says a new participant mid-recording pauses recording until fresh consent is logged. Options: (a) the doctor flags it manually — simple, depends on the doctor remembering; (b) trust ASR diarization to detect a new speaker — automatic, but diarization invents and merges speakers constantly, so you'll get false pauses mid-consult; (c) manual flag now, revisit automation after you've seen real diarization output from your own clinic audio. My read is (c), because you have no Taglish diarization data yet and (b)'s failure mode interrupts a live medical exam. But this is a product-risk call, so make it deliberately.

### 0.2 Actually enforce RBAC ⚠️

- [x] Apply `require_role(...)` to routes. Right now it is defined in `app/api/deps.py` and used on **zero** endpoints.
- [x] Decide per-route: who can read a note? Only the authoring clinician, or any clinician in the clinic?
- [x] Test: a `compliance`-role token must not be able to `PATCH` a note; a `doctor` token must not be able to read the audit log.

⚠️ **Heads-up:** a dependency that is written but never attached is worse than one that doesn't exist — it reads like coverage in a code review and provides none. Grep for `require_role` before you trust the docstring in `models/clinician.py` that says role "drives" access control. It currently drives nothing.

### 0.3 Make auth survive a real clinic day 🧠

- [x] Add refresh tokens with rotation, or extend session lifetime deliberately.
- [x] Add an MFA enrollment endpoint (provision secret → return provisioning URI/QR → confirm with one valid code before activating). Today the TOTP secret can only be created by a seed script.
- [x] Add rate limiting on `POST /auth/login` (per-IP and per-email).
- [x] Add account lockout or exponential backoff after repeated failures.

⚠️ **Heads-up:** `ACCESS_TOKEN_EXPIRE_MINUTES=30` with no refresh path means a doctor gets logged out mid-consultation, roughly twice per clinic session. Discovering this in a pilot rather than now would poison the "voluntary use in week 4" metric for a reason that has nothing to do with note quality.

🧠 **Your call — where does the token live on the device?** Options: `expo-secure-store` (Keychain/Keystore-backed, survives app restart, the standard answer) vs in-memory only (safest against device compromise, forces re-login every launch). For a clinical app on a doctor's own device, secure-store plus a short-lived access token plus biometric re-auth on resume is the usual balance. Consider what happens to the token if the phone is lost — is there a server-side revocation list, or do you just wait for expiry?

### 0.4 Fix the type and consistency drift

- [x] Convert `Encounter.pipeline_status` from a free-form `String(32)` to a proper enum, the way `Note.status` already is. It currently accepts any string, and the codebase writes at least five different values across two files.
- [x] Move `confirm_upload`'s `audio_object_key` from a query parameter into a Pydantic request body.
- [x] Add a `CHECK` constraint or enum for `ConsentLedgerEntry.event` (`given|declined|withdrawn`).

📚 **Understand first:** why enums-at-the-DB-layer matter more here than in a typical CRUD app. Both `Note.status` and the consent ledger are *legal* records. "The database physically cannot hold an invalid value" is a much stronger statement to an auditor than "our code only ever writes valid values." That's the same reasoning behind the append-only trigger — push the guarantee as far down the stack as it will go.

### 0.5 Close the test-vs-production divergence ⚠️

- [x] Add a Postgres-backed test path (testcontainers, or a CI service container) for the tests that depend on Postgres-specific behavior.
- [x] Write a test proving the consent ledger rejects `UPDATE` and `DELETE`.

⚠️ **Heads-up — this one is sharp.** The test suite runs on SQLite via `Base.metadata.create_all()`. The append-only consent trigger lives in an Alembic migration. **Migrations never run in the test suite, so the trigger is never exercised by a single test.** I verified it manually with `psql`, which is why I know it works — but manual verification is not a regression test. Someone could drop that migration tomorrow and every test would still pass. Any guarantee implemented in SQL rather than Python is currently untested by construction.

📚 **Understand first:** "test against what you deploy." SQLite-for-speed is a common and often reasonable trade, but it silently voids every Postgres-specific guarantee: triggers, `pgcrypto`, native enums, `CHECK` constraints with Postgres semantics, concurrent-transaction behavior. Know exactly which of your guarantees fall in that blind spot, and cover those on real Postgres.

---

## Phase 1 — Make the pipeline real

Goal: audio recorded on a device ends up as a structured note in Postgres, with no human in the loop.

### 1.1 Upload path 🧠 📚

- [x] Implement an S3/MinIO client module (`app/services/storage.py`) — `boto3` is already a declared dependency and currently unused.
- [x] Implement chunked, resumable upload. Endpoints, roughly: `POST /encounters/{id}/upload/init` → `PUT /encounters/{id}/upload/chunk/{n}` → `POST /encounters/{id}/upload/complete`. (Presigned multipart shape: `POST .../upload/init` → `POST .../upload/parts/{n}` mints a presigned URL the device PUTs to directly → `POST .../upload/complete`.)
- [x] Persist per-chunk state so a resumed upload skips what already landed. (S3's own `ListParts` is the persisted state — `GET .../upload/parts` — rather than a mirrored Postgres table; see decision 0013.)
- [x] Enforce the idempotency key across the whole flow, not just encounter creation. (`upload/init` and `upload/complete` are both idempotent on retry — see docs/progress/1.1.)
- [x] Server-side encryption at rest on the bucket, plus a lifecycle policy keyed to `AUDIO_RETENTION_DAYS`.

🧠 **Your call — build the upload protocol or adopt one?** Three real options:
- **S3 multipart with presigned URLs.** The device uploads directly to object storage; your API only mints URLs and gets a completion callback. Cheapest to run, least bandwidth through your server, natively resumable. Cost: presigned-URL scoping is easy to get subtly wrong, and your API no longer sees the bytes (so it can't enforce anything about them).
- **[tus.io](https://tus.io) resumable protocol.** A real spec with mature client and server implementations, designed for exactly this. Cost: another moving part to run and understand.
- **Roll your own chunk endpoints.** Total control, matches the PRD's wording directly, and you'll understand every failure mode because you wrote them. Cost: you will reimplement bugs the other two already fixed — partial-chunk corruption, concurrent resume, orphaned uploads.

For learning value, rolling your own once is genuinely instructive. For a 4–8 week clinical MVP, presigned multipart is the pragmatic answer. If you roll your own, at minimum handle: chunk checksums, out-of-order arrival, and an orphan-upload reaper.

📚 **Understand first:** why idempotency keys exist at all. A phone on clinic wifi will retry a request whose response it never saw. Without a key, "retry" and "second consultation" are indistinguishable to your server, and you get duplicate notes on the same patient — a clinical-safety bug, not just a data bug. Trace the key's path through `encounters.py` and convince yourself where a duplicate could still slip through today.

⚠️ **Heads-up:** local audio must only be deleted after the server confirms *both* receipt and that note generation has begun (P0-2). Deleting on upload-complete alone means a server-side pipeline crash loses the consultation permanently. The confirmation the device waits for should be about the pipeline, not the bytes.

### 1.2 Transcript persistence 🧠

- [x] Add a transcript model/table (or object-storage document) holding: full text, per-word timings, per-word confidence, and speaker labels.
- [x] Make `transcribe_encounter` actually persist its output. It currently computes `segments` and discards them with `_ = segments`.
- [x] Make `generate_note` load the persisted transcript instead of passing `transcript=[]`.

🧠 **Your call — where does the transcript live?** Options:
- **Postgres `JSONB` column.** Queryable, transactional with the note, encrypted with your existing `EncryptedString` approach if you wrap it. Cost: word-level data for a 20-minute consult is large; you'll be loading megabytes to render one note.
- **Row-per-word table.** Precise, indexable by time, ideal for the grounding UI's "play from here." Cost: hundreds of thousands of rows per clinic-week and a heavier write path.
- **Object storage, like the audio.** Cheap, unlimited size. Cost: not queryable, another fetch on the read path, and a second place PHI lives that retention must remember to purge.

This choice largely determines how hard Phase 3 (grounding UI) is, so think about that requirement now rather than after. My instinct is `JSONB` for the MVP with the *sentence* as the addressable unit, because it keeps one transactional home for one note's data — but if you want word-precision audio seeking, the row-per-word table stops being overkill.

⚠️ **Heads-up:** the transcript is PHI, arguably more sensitive than the note (it's verbatim, including things the doctor chose not to record). Whatever you pick, it needs the same encryption, the same access logging, and the same retention clock as the audio. A retention job that purges audio and leaves transcripts is not a retention policy.

### 1.3 Real ASR integration ⚠️ 🧠

- [x] Implement `ElevenLabsScribeProvider.transcribe` — stream the object from storage, POST to Scribe v2 with diarization enabled. (Vendor changed to Groq-hosted Whisper large-v3, the user's call — no diarization capability at all as a result. See decision 0018.)
- [x] Handle rate limits, timeouts, and partial failures with Celery retries (already scaffolded via `self.retry`).
- [x] Record which ASR provider and model version produced each transcript.
- [x] **Fix `_parse_response`** — see the heads-up below. (N/A in the form asked: Whisper has no speaker labels to mis-group by, so the original bug's *mechanism* can't recur — but turn order is still explicitly preserved by construction; see docs/progress/1.3.)

⚠️ **Heads-up — there is a real bug in the stub I wrote.** `_parse_response` groups every word by speaker across the entire recording, producing one giant segment per speaker. That destroys turn order: you get "everything the doctor said" then "everything the patient said," instead of the actual back-and-forth. A note generated from that will mangle who reported which symptom. Segments must be *turns* — split when the speaker label changes. Worth reading that function and seeing the bug yourself before fixing it; it's a good example of code that looks reasonable and is semantically wrong.

⚠️ **Heads-up — superseded, kept for the reasoning.** This originally warned that Scribe returns anonymous `speaker_0`/`speaker_1` labels you'd have to map to doctor/patient yourself. That problem no longer exists in the form described here — the ASR vendor in use (Groq-hosted Whisper, decision 0018) has no speaker labels at all, anonymous or otherwise, so there's nothing to map. The actual heuristics below (doctor speaks first, doctor speaks the consent script, doctor has more speech time) are gone as a *mapping* tool but survive as a **content-inference** tool: if a diarization step is ever added back (decision 0018's options), or if Phase 1.4's note generation has to infer speaker roles from undiarized text alone, these are the same signals to reach for either way.

🧠 **Your call — how do you validate ASR quality with no bake-off?** The roadmap explicitly dropped the vendor bake-off and accepted this risk, making internal alpha the first real test. So decide now what you'll measure and how: a small set of consented recordings hand-transcribed as ground truth? Clinically-weighted entity error rate on drug names and doses (as the PRD's success metrics suggest)? Doctor-reported "did you have to fix a name/dose" flag on each note? Pick something cheap and start collecting from day one of alpha — the risk register says this surfaces via edit burden, and edit burden is only measurable if you instrumented it before the first note.

### 1.4 Real note generation ⚠️ 🧠

- [ ] ~~Implement `LunaNoteGenerator.generate`~~ — **OBSOLETE.** Decision 0021 (2026-08-25, the user's call): Haiku is the sole note generator; Luna is deleted, not kept as a dormant fallback. `luna.py` no longer exists.
- [x] Implement `HaikuNoteGenerator.generate` — single fused call (P0-4), APSO section order, hedged language, silence/low-confidence suppression. (No longer "the configured fallback" — it's the only provider. See decision 0021 for what that costs: P0-4's fallback-as-risk-mitigation no longer exists.)
- [x] Use structured output (JSON schema / tool call), not free-text parsing. (A forced `tool_choice`, not a hopeful one — the model cannot answer any way but the schema.)
- [x] Pass word-level confidence into the prompt in a form the model can act on. (Not "act on" as in "please be careful" — words below `NOTE_GENERATION_LOW_CONFIDENCE_THRESHOLD` are physically replaced with `[INAUDIBLE]` before the prompt is built, per the heads-up below.)
- [x] Store the prompt version alongside each generated note. (`Note.prompt_version`, `b8c9d0e1f2a3_note_prompt_version.py`.)
- [x] Add a golden-transcript test suite: fixed transcript in, assertions on the note out. (`tests/test_note_generation_haiku.py::test_generate_end_to_end_with_a_mocked_response`.)

⚠️ **Heads-up:** "generation is suppressed over silent or low-confidence windows" (P0-4) will not happen just because your system prompt says so. Models are strongly biased toward producing fluent, complete-looking clinical text. If you hand it a transcript with a garbled 30-second stretch, it will smooth over the gap plausibly and you will not be able to tell. Make suppression *mechanical* where you can: mark low-confidence spans in the input explicitly (e.g. `[INAUDIBLE 0.31]`), and validate the output for invented content rather than trusting instruction-following. **Followed, two layers deep:** `_format_transcript` replaces any word below `note_generation_low_confidence_threshold` with a literal `[INAUDIBLE]` before the model ever sees it (the model cannot smooth over a gap it was never shown), and the schema forces the model to set a per-section `suppressed` boolean explicitly rather than letting the code infer it from empty text — and `suppressed=true` forces `text=""` server-side even if the model inconsistently also emitted sentences.

⚠️ **Heads-up:** storing the prompt version per note matters more than it sounds. When edit burden jumps in week 3, the first question is "did we change the prompt?" — and without a version stamped on each row, that question is unanswerable after the fact. **Followed:** `PROMPT_VERSION = "haiku-v1"` in `haiku.py`, stored on `Note.prompt_version` on every generation — bump the constant whenever the system prompt or tool schema meaningfully changes.

🧠 **Your call — how do you get trustworthy source spans?** P0-4 requires every generated line to trace back to its transcript passage, and P0-7 builds a UI on that. But **an LLM asked to emit character offsets will produce confident, wrong numbers** — it cannot count characters reliably. Options:
- Have the model **quote** the exact supporting passage verbatim, then string-search the transcript server-side to compute real offsets. Slower, more tokens, but the offsets are ground truth.
- Give each transcript sentence a stable **ID** in the prompt and have the model cite IDs. Cheap, robust, coarser granularity.
- Ask for offsets directly. Cheapest, and I'd expect it to be wrong often enough to break the feature.

I'd take sentence IDs: it's the only option that's both cheap and verifiable, and sentence-level grounding is almost certainly enough for a doctor scanning for "where did this come from?"

**Resolved (implementation-level judgment call, decision 0022):** segment IDs — but the *already-persisted* transcript segment ID from Phase 1.2 (decision 0016), not a new sentence-numbering scheme built just for this. The model cites `segment_ids` in its tool-call output; any ID that doesn't match a segment actually sent in the prompt is dropped before the note is saved, not trusted (`HaikuNoteGenerator._build_section`). `text_start`/`text_end` — offsets into the note's own generated text, not the transcript — are never asked of the model at all; the server computes them exactly, by tracking a cursor while concatenating the model's own per-sentence output.

📚 **Understand first:** the difference between a model *citing* and a model *appearing to cite*. Any output can contain a plausible-looking reference. Only a reference you independently verified against the source is evidence. This distinction is the whole reason the grounding UI is a P0 requirement rather than a nice-to-have — it's the doctor's mechanism for catching exactly this failure.

### 1.5 Pipeline failure handling

- [x] Give the doctor a specific, actionable error state per failure mode (upload failed / transcription failed / generation failed) — the PRD's edge case explicitly rejects "a silent gap in the record." *(Done, minus `upload_failed` — decision 0023 explains why that one specifically doesn't need a persisted state. `TRANSCRIPTION_FAILED`/`GENERATION_FAILED` are real `EncounterPipelineStatus` members now, plus `retry_count`/`last_pipeline_error` on `EncounterOut` so a client has an actual message, not just a state name.)*
- [x] Add a dead-letter path: after max retries, mark the encounter failed and surface it in the app. *(Done — `_mark_stage_failure` in `app/tasks/pipeline.py`; surfaced via `GET /encounters/failed` since there's no app yet to surface it in.)*
- [x] Never leave an encounter stuck in an intermediate `pipeline_status` with nothing watching it. *(Done — `sweep_stuck_encounters`, Celery Beat, every 5 min. See the resolved 📚 note below: this is deliberately a second, separate mechanism from dead-lettering, not the same one applied twice.)*
- [x] Add a "regenerate note" action for transient failures. *(Done, generalized to both failure stages — `POST /encounters/{id}/retry` re-runs only the stage that failed, not the whole pipeline; see decision 0023 for why re-transcribing on a `GENERATION_FAILED` retry would be wasted real money.)*

📚 **Understand first:** a queue system's real hard part isn't throughput, it's *stuck work*. Every async pipeline eventually has jobs that neither succeeded nor failed loudly. Decide now how you'll notice: a periodic sweep for encounters older than N minutes in a non-terminal state is the usual answer, and it's much easier to add before you have production data than after.

**Followed, and worth being explicit about why it's not the same code path as dead-lettering (decision 0023):** dead-lettering only fires for a task that actually ran and raised an exception. It structurally cannot catch a task that never ran at all — broker down, or the worker pool at zero, at the exact moment the pipeline was kicked off. `sweep_stuck_encounters` catches that other case by comparing a `pipeline_updated_at` timestamp against a configurable staleness threshold instead of by catching anything — there's nothing to catch. Re-kicking a merely-slow (not actually stuck) encounter is safe because both tasks were already idempotent no-ops on already-done work, from Phase 1.2/1.3 — the sweep only has to be safe when wrong, not perfectly accurate about what's really stuck.

---

## Phase 2 — Build the mobile app

This is the largest phase and currently 0% done. Everything above is invisible to a doctor without it.

### 2.1 App foundation

**Client re-platformed to a browser web app on a clinic laptop — decision 0024.** The supervisor answered the PRD's open question ("what devices do doctors actually carry": laptops), which collapsed both reasons `tech-stack.md` gave for rejecting a web client. Items below are rewritten accordingly; `apps/mobile/` is deleted (git history retains it), `apps/web/` replaces it.

- [x] ~~Navigation (`expo-router` or React Navigation)~~ → **React Router**, `apps/web/src/App.tsx`, with an auth-gated route split and a `checking` state so a reload never flashes the login screen at an already-signed-in doctor.
- [x] Generate a typed API client from FastAPI's OpenAPI schema rather than hand-writing fetch calls. This is the payoff for the Python-backend/TypeScript-client split described in `docs/tech-stack.md`. (**Done, and it survived the re-platform untouched — the strongest item in this phase.** `openapi-typescript` + `openapi-fetch`; 1583 lines of types generated from the live schema's 22 paths / 31 components into `src/api/schema.d.ts` via `npm run api:types`. A renamed backend route now breaks `tsc`, not a clinic.)
- [x] Auth flow: login → TOTP → secure token storage → ~~biometric unlock on resume~~. (Login is single-step: the API takes email + password + TOTP together and returns the same 401 whichever factor failed. Token storage is *better* than the mobile plan could manage — see the Understand-first note below. **Biometric unlock is deliberately deferred, not done — see the open question below it.**)
- [x] Error/offline UI primitives you'll reuse everywhere. (`components/Banner.tsx`, `ErrorBoundary.tsx`, `lib/offline.ts` — persistent banners, not toasts, because a toast that vanishes in three seconds *is* silent failure for a doctor who was looking at a patient.)
- [x] ~~Build a real dev client (`npx expo run:android`)~~ — **OBSOLETE, and this is the phase's biggest saving.** This item existed solely to host the native audio modules a phone needed for background capture. With a laptop it does not exist rather than being solved.
- [x] **NEW — CORS + httpOnly refresh cookie on the API.** Two things a browser client needs that a native one never did, and both fail *silently*: without CORS the preflight is rejected and the request never reaches a route, so nothing appears in the API log at all. Covered by 10 tests in `tests/test_web_client_support.py`.

⚠️ **Heads-up — superseded, kept for the reasoning.** This warned that the Android/iOS toolchain is "a genuine time sink... the most common place a mobile-first plan slips." Still true of mobile, and now moot: there is no toolchain, no store, no signing, and no Mac requirement. Worth keeping visible because it is *why* the re-platform paid for itself immediately — and because it returns in full if the laptop premise ever fails (decision 0024's "what would change my mind").

🧠 **Your call — biometric unlock on a *shared* laptop.** The original item assumed a personal phone, where biometric unlock is unambiguous. A shared clinic laptop breaks that assumption: Windows Hello / Touch ID via WebAuthn authenticates *the machine's logged-in user*, so if several doctors share one Windows session, a biometric prompt proves nothing about which doctor is signing a note. Options:
- **Per-doctor OS accounts**, after which WebAuthn works as intended. Cleanest, but an IT policy decision rather than a code one.
- **Short idle-lock requiring password + TOTP re-entry.** Weaker UX, correct on a shared login, needs no WebAuthn at all.
- **WebAuthn regardless**, accepting that it identifies the device rather than the clinician — which is arguably worse than nothing, because it *looks* like proof of identity.

I'd want to know whether doctors share a Windows login before building any of them; the answer decides which is even coherent. Deferred rather than guessed, and nothing else in Phase 2 depends on it. Note this interacts with signing (2.6), where "who signed this note" is a medico-legal question, not a UX one.

📚 **Understand first — why the token storage got *better*, not worse.** The mobile plan put the refresh token in `expo-secure-store`, which is readable by app code and therefore by anything achieving code execution in the app. The browser has something the phone did not: an **httpOnly** cookie, which JavaScript cannot read at all. So the access token stays in memory (never `localStorage`) and the refresh token lives in a cookie this codebase cannot see. The payoff shows up in the flow: after a full page reload there is no access token, the client calls `/auth/refresh` with an *empty body*, and the **server** reads the cookie — which is how "resume after reload" works without ever persisting a credential anywhere JS can reach. Decision 0006 is amended, not reversed.

⚠️ **Heads-up — a client-side hazard created by Phase 0.3's own security.** Refresh tokens rotate on every use, and a replayed one is treated as theft and revokes the whole session family. So two *concurrent* refreshes are self-harm: the second presents a token the first already rotated, reuse detection fires, and the client logs the doctor out mid-consultation. The fix is a single shared in-flight promise so N concurrent 401s produce exactly one refresh (`refreshSession` in `src/api/client.ts`). This is not an optimization, and it is invisible until you have two parallel requests — which an upload queue guarantees.

### 2.2 Recording — the hard part 📚 ⚠️ 🧠

- [x] One-tap record with a persistent, always-visible recording indicator (P0-1). (`components/RecordingIndicator.tsx` — sticky, undismissable, `role="status"`, and it surfaces missing audio inline rather than in a details panel. It is a legal control under RA 4200, not a status chip.)
- [x] Background capture that survives app backgrounding, screen lock, and incoming calls (P0-2). **Re-scoped to the laptop and partly already measured (decision 0024):** backgrounding and screen lock are *empirically satisfied* — 0.05s of audio lost across 131s hidden over 9 windows. "Incoming call" is essentially N/A. **Lid close / system sleep is not satisfied and is unsatisfiable in software** — it cost 6.5s of real audio in the harness run, and it is OS power policy that neither a browser nor Electron can veto. Mitigation is device config (Windows: "When I close the lid → Do nothing") plus chunked IndexedDB writes so a suspend truncates rather than destroys.
- [ ] ~~Android: foreground service with an ongoing notification.~~ **OBSOLETE** — no Android app (decision 0024).
- [ ] ~~iOS: `UIBackgroundModes: audio` plus correct `AVAudioSession` category.~~ **OBSOLETE** — no iOS app (decision 0024).
- [x] Encrypt audio on-device *before* it touches disk, key sealed in ~~Keychain/Keystore~~ **a non-extractable Web Crypto `CryptoKey` in IndexedDB**. (Verified, not assumed: the end-to-end test reads the browser's own IndexedDB and asserts the WebM magic bytes are absent from what was stored — if plaintext audio ever reaches disk, that check fails.) Note the honest downgrade this represents: a browser cannot seal a key in a hardware keystore the way Keychain/Android Keystore could. If Legal requires hardware-sealed key custody for on-device PHI, decision 0024's option (b) — an Electron wrapper — becomes necessary, and this is the item that forces it.
- [x] Handle interruptions: ~~pause/resume on phone call~~, **save partial audio on crash**. (Crash-safety done: 5s chunks land in IndexedDB as they go, so a crash or suspend costs at most one chunk, and `requestData()` flushes MediaRecorder's partial chunk on stop — without it the tail of the consultation, often where the plan is stated, is discarded. "Phone call" is N/A on a laptop; a *deliberate* pause for a mid-visit interruption is a real need and belongs with 2.3's re-consent flow, which is what makes pausing legally meaningful.)
- [x] Write audio in chunks as you go, never buffering a whole consult in memory. (~5s / ~20 KB chunks — deliberately *unrelated* to S3's 5 MB minimum part size, which at 32 kbps is ~21 minutes of audio. Sizing chunks to the S3 minimum would risk 21 minutes per crash, the opposite of what a write-ahead log is for. See decision 0026.)

- [x] **NEW — client-side consent gate (P0-1).** P0-1 says the app must block recording *"before anything is captured"*, and the existing server enforcement (upload confirmation, transcription) both run *after* capture. New `GET /api/v1/consent/{encounter_id}`, built on the same ledger fold `assert_consent_valid` uses so the read and the enforcement cannot disagree — asserted directly across five ledger sequences. Fails closed on every uncertain path including offline, and re-checked at the moment of the tap, not only on mount. See decision 0026 for the cost of the offline choice.

⚠️ **Heads-up — audio gaps are now *recorded*, because they cannot be prevented.** Decision 0024 measured lid-close costing 6.5s of real audio, and established that OS power policy beats any client architecture. Given that, the only honest design is to detect the loss and say so: an AudioWorklet counts samples as ground truth (codec-independent, and unlike byte-counting it is not fooled by Opus encoding silence to nearly nothing), wall-clock jumps are read as suspends, worklet silence as stalls, and missing time appears **in the recording indicator itself**. Note the anchoring subtlety, which was a real bug in the harness before it was fixed: the measurement starts at the first worklet message, not the button press — the ~0.7–1.3s between them is startup latency, and charging it to "missing audio" produced a false loss warning on a perfectly healthy run.

📚 **Understand first:** mobile OSes treat background microphone access as adversarial by default, and reasonably so. Both platforms will kill or silence a backgrounded recorder unless you declare intent through a specific mechanism — a foreground service on Android, a background mode on iOS — and both require user-visible indication. This is precisely why `docs/tech-stack.md` rules out a PWA: browsers give you no such mechanism at all. Read the platform docs for background audio before writing this; it's the difference between a day and a week.

⚠️ **Heads-up — OBSOLETE.** "Apple review will ask why a medical app records in the background" — there is no App Store submission (decision 0024). Kept because the underlying point survives the platform change: the clinical justification for recording still needs writing down, and the consent flow is still the thing that satisfies whoever asks. That is now Legal and Remedy's DPO rather than Apple.

⚠️ **Heads-up — mostly defused, not fully.** This warned about battery/thermal over a clinic day on a mid-range Android. The PRD's open question is now answered (laptops, decision 0024) and a plugged-in laptop removes most of the risk — and at mono Opus 32 kbps a 30-minute consult is ~7 MB, not a big file. What remains genuinely unmeasured: an actual 8-hour day of intermittent recording on the real clinic laptop. "Far less pressing" is not "measured".

🧠 **Your call — audio format and bitrate.** You're trading ASR accuracy against file size against upload time on clinic wifi. Speech at 16 kHz mono in a compressed format (AAC/Opus) is usually plenty for ASR and dramatically smaller than uncompressed WAV. But verify against Whisper's documented input expectations before committing (Groq's hosted large-v3, decision 0018 — not Scribe, since the ASR vendor changed in 1.3), and test whether aggressive compression measurably hurts Taglish accuracy — code-switched speech may be less robust to compression artifacts than monolingual English.

**Resolved (decision 0025, the user's call): mono Opus at 32 kbps.** Brought forward from 2.2 into 2.1 because the capture harness produced a number worth reacting to before any recorder existed: its 29-minute run recorded **129 kbps stereo, 26.7 MB**, having silently ignored a `channelCount: 1` constraint. That is roughly 550 MB per clinic day over clinic wifi versus ~115 MB at the chosen setting, for no accuracy gain — Whisper resamples to 16 kHz mono internally regardless. Constants live in `apps/web/src/lib/audio-config.ts`. Critically the implementation does not *trust* the constraint: it also sets `audioBitsPerSecond` explicitly (without which the browser picks, which is exactly how 129 kbps happened) and calls `assertAudioSettings()` to surface any requested-vs-actual mismatch loudly rather than silently.

### 2.3 Consent flow (P0-1)

- [x] Consent screen presenting the script in Filipino and English, blocking recording until resolved. (`routes/Consent.tsx`. **The script text is a placeholder written by an engineer and is not cleared by counsel** — the app says so on screen, and the text is isolated in `lib/consent-script.ts` so counsel's version is a single edit. RA 4200 clearance is the PRD's own blocking open question.)
- [x] Capture the participant roster before recording starts. (Doctor and Patient locked as always-present, extras opt-in — RA 4200 needs the consent of *every* party, so each is named on the ledger entry.)
- [x] Record the spoken consent exchange as the first segment of the audio file. (**Read P0-1's two bullets together before touching this** — see the 📚 note below. The ordering is forced: log consent, *then* start recording, then speak the confirmation. The consent screen never touches the microphone.)
- [x] Handle decline gracefully — the app must remain fully usable without recording (explicit PRD edge case). (And decline is deliberately unreachable until the script has been presented: logging a decline the patient was never read would claim an informed refusal that did not happen.)
- [x] Mid-visit re-consent: pause recording, capture new roster, log a new ledger entry, resume. (Manual flag — **decision 0003 is now closed by elimination**, since decision 0018 removed diarization entirely. The pause happens *before* any network call because it is the compliance action; resuming is gated on the ledger write succeeding, not on the doctor's word. See the ⚠️ below for the three ways pause interacts with 2.2's gap detection.)
- [x] Withdrawal action, available at any time, that reaches the server. (Client: stop capture → delete local chunks → tell the server, so a failed network call leaves *less* data behind, not more. Server: ledger entry committed first, retention clock set to now as the durable backstop, then a best-effort immediate object delete.)

⚠️ **Heads-up — addressed, and the advice was followed exactly.** This warned that withdrawal had *no* server-side effect, and that the honest design is "stops at the next checkpoint" rather than "stops instantly". Both now hold: `handle_withdrawal` sets the retention clock to now (durable backstop) and attempts an immediate object delete (best-effort), while the pipeline stop relies on Phase 0.1's consent re-checks at upload confirmation and at the head of `transcribe_encounter`. **No attempt is made to kill a running Celery task**, and the UI wording says "next stage boundary, not instantly" — asserted by a smoke check, because that sentence is also what Legal will be told the system does.

📚 **Understand first — P0-1's first two bullets constrain each other, and reading either alone gets it wrong.** Bullet 1 says the script is presented *"before anything is captured"*. Bullet 2 says that once consent is given, *"the spoken exchange is captured as the first segment"*. The tempting reading — start recording, read the script, and the recorded asking becomes segment 1 — satisfies bullet 2 and **violates bullet 1**. The only sequence satisfying both is: roster → read the script → log the outcome → start recording → speak a short confirmation, which becomes segment 1. A consequence worth being explicit about with Legal: the patient's own spoken "yes" is therefore *not* on the recording, only the doctor's confirmation that it was given. Putting it on tape would require recording before consent is logged, which is a decision someone with authority has to make.

⚠️ **Heads-up — pausing is not just `MediaRecorder.pause()`; it collides with 2.2's gap detection three ways.** Each would have produced a confidently wrong reading: (1) the AudioWorklet keeps counting samples the recorder is no longer writing, so the pause reports as *lost audio* — fixed by suspending the `AudioContext` too, stopping both clocks together; (2) the stall detector fires, because worklet silence is its symptom and during a deliberate pause that silence is expected — so the monitor skips stall/suspend detection while paused; (3) the pause duration reads as a wall-clock jump, i.e. a "system suspend" gap — the recorder logging a fault it caused itself — so both watchdog baselines reset on resume. And one that silently eats audio: **a paused `MediaRecorder` ignores `requestData()`**, so `stop()` must resume before flushing or the buffered tail is discarded.

### 2.4 Offline queue (P0-2)

- [x] Durable queue surviving app kill and device restart — **IndexedDB**, not `expo-sqlite` (decision 0024). Same write-ahead-log invariant, different store. (Schema v2, `uploads` store beside the audio chunks — one database, one version, one guarded additive upgrade handler so a laptop mid-pilot upgrades without losing queued audio. A crashed recording is recovered on next launch and uploaded: partial audio beats none.)
- [x] Visible, persistent queue status — nothing may fail silently (explicit PRD requirement). (`components/QueueStatus.tsx`, on both the recording screen and the worklist. **Two bugs in this readout were found by the end-to-end test's own output rather than its assertions** — see the ⚠️ below; both passed the original suite while being visibly wrong in the log.)
- [x] Background upload with exponential backoff. (5s doubling, capped at 5 min, **jittered** so several laptops recovering from one wifi outage do not hit the API in synchronised waves. Crucially, `OfflineError` does *not* consume the attempt budget — counting an outage toward the retry ceiling would dead-letter healthy recordings during exactly the event this queue exists to survive.)
- [x] Generate the idempotency key on-device at recording start, and persist it before the first byte is uploaded. (Asserted directly end-to-end: at t+0.9s the queue entry exists in state `recording` with its key persisted, and **zero chunks are on disk** — the record of intent genuinely precedes the data.)
- [x] Delete local audio only after the server confirms receipt *and* pipeline start. (A distinct `uploaded → confirmed` step polls `GET /encounters/{id}` and advances only at `transcribed`/`note_generated`. See the 📚 note below for why the 200 on `upload/complete` is not enough. A *terminal server failure* keeps the local copy — it may be the only one.)
- [x] Handle the device-full case. (Checked *before* recording starts, because a `QuotaExceededError` halfway through a consultation loses the rest of it with no graceful recovery. Reported as **minutes of recording remaining**, not a percentage: "8% free" means nothing to a doctor, "about 20 minutes left" is directly comparable to a consultation.)

📚 **Understand first:** this is a write-ahead log, the same pattern databases use for durability. The invariant is that the *record of intent* is committed to durable storage before the risky operation begins, so a crash at any point leaves you able to reconstruct what should happen. If you generate the idempotency key in memory and crash before persisting it, the retry generates a new key and you get a duplicate — which is exactly the bug the key exists to prevent.

📚 **Understand first — "receipt" and "pipeline start" are different events, and only one of them is a 200.** `upload/complete` returning 200 confirms that S3 holds the object and a Celery chain was *enqueued*. It says nothing about whether a worker ran. A broker outage or an empty worker pool — the precise scenario Phase 1.5's stuck-sweep exists for — leaves `pipeline_status` at `uploaded` indefinitely, and deleting on the 200 would destroy the only copy of a consultation whose processing never began. So the queue has a separate `uploaded → confirmed` step that polls the encounter and advances only at `transcribed`, which is both the first status proving work happened *and* the point the transcript exists server-side, so audio stops being the sole record of what was said. The mirror case matters too: a *terminal* server failure (`transcription_failed`, `blocked_no_consent`) must **keep** the local audio, since it may be the only copy and Phase 1.5's `/retry` can still use it.

⚠️ **Heads-up — the queue's status readout is the easiest thing here to get quietly wrong.** Two bugs were caught by reading the end-to-end test's own output, not by its assertions; both left the upload working while making the status lie, and the status *is* the P0-2 requirement. (1) `recoverInterrupted` ran every tick and could not distinguish "the app crashed mid-recording" from "recording is happening right now in this tab" — so a normally-stopped 14-second recording was labelled *"Recording was interrupted"* and queued for upload **while still capturing**, risking chunks written after the upload being deleted unsent. Fixed with a heartbeat plus a staleness window; a timestamp rather than an in-memory flag, because it must survive the process dying, which is the case being detected. (2) The byte total was taken from React state captured *before* `stop()` flushed MediaRecorder's final chunk, so the panel showed *"56 KB of 37 KB"* — progress over 100%. Now derived from the chunk store, which is the only thing that knows what is on disk. Both have regression assertions; neither would have been noticed by a passing suite.

### 2.5 Patient identity (P0-6)

- [ ] Patient search by typed or dictated name.
- [ ] Exact match links silently; near match requires one-tap confirmation; no match offers create-new.
- [ ] Loose-sessions tray for recordings started before a patient was selected.
- [ ] Re-confirm patient identity at the moment the note is filed, not only at recording start.

⚠️ **Heads-up — an architectural consequence you'll hit immediately.** `Patient.full_name` is encrypted at rest via `EncryptedString`, which means **the database cannot search it.** Ciphertext isn't comparable, so `match_patient` filters by exact birthdate first and only then compares names in Python. The PRD's UX wants name-first search — and name-first search over encrypted columns is impossible without help.

🧠 **Your call — how do you get searchable encrypted names?** Options:
- **Blind index:** store an HMAC of the normalized name alongside the ciphertext, and search that. Enables exact and prefix matching while the name stays encrypted. Standard solution; adds a second key to manage, and leaks equality (you can tell two patients share a name without decrypting).
- **Don't encrypt the name;** rely on DB-level and disk-level encryption instead. Simplest, searchable, weaker — a Postgres read is now a PHI read.
- **Keep birthdate-first matching** and design the UX around it (front-desk check-in queue, PRD's own P1 item, sidesteps search entirely).

Also worth thinking through: a mistyped birthdate currently means dedup silently fails and you create a duplicate patient — which puts one person's history in two records. What's your recovery path? A merge tool is unglamorous and you will need it.

### 2.6 Review, edit, sign (P0-5)

- [ ] Note review screen, Assessment → Plan → Subjective → Objective order.
- [ ] Free editing of any section pre-signing, with each edit recorded as a `NoteRevision`.
- [ ] Explicit stepwise state transitions — no skipping (server already enforces this).
- [ ] Signing ceremony: deliberate, distinct, capturing name + PRC license + timestamp.
- [ ] Objective-findings entry for things never spoken aloud.
- [ ] Show the prior visit's assessment and plan for longitudinal context.

🧠 **Your call — what counts as a "minor edit"?** The PRD's headline quality target is "≥70% of signed notes require only minor edits," so this definition literally determines whether you pass. Character-level edit distance? Word-level? Clinically-weighted (a changed dose counts more than a rephrased sentence)? Decide before alpha, write it down, and compute it consistently — a metric redefined mid-pilot tells you nothing.

⚠️ **Heads-up:** `NoteRevision` stores full before/after text per edit. Every revision is another encrypted copy of PHI, and they compound fast. Make sure retention covers revisions too, and consider whether you need every keystroke-level revision or just per-save snapshots.

---

## Phase 3 — Grounding UI (P0-7)

- [ ] Tap a note line → highlight the source transcript passage.
- [ ] Tap again → play audio from that timestamp.
- [ ] Serve audio to the device without permanently re-downloading PHI (short-lived presigned URLs, range requests).
- [ ] Handle the case where audio has already been deleted by retention but the note remains.

📚 **Understand first:** this feature is the product's trust mechanism. The doctor's rational response to "an AI wrote this" is "prove it," and grounding is the proof. Everything upstream — span storage, sentence IDs, verbatim quoting — exists to make this screen honest. If you cut corners in 1.2 or 1.4, this is where it shows.

⚠️ **Heads-up:** notes outlive audio. Retention will delete the recording while the signed note is a permanent medical record. The grounding UI must degrade gracefully to "transcript only" and then to "source no longer retained" — and the doctor should understand which state they're in, not just see a dead play button.

---

## Phase 4 — Security and compliance hardening (P0-8)

### 4.1 Encryption and key management 🧠 ⚠️

- [ ] Decide and document the PHI-at-rest approach.
- [ ] Key rotation procedure, written and rehearsed.
- [ ] Separate keys per environment; production keys never on a developer machine.
- [ ] TLS everywhere, HSTS, modern cipher suites.

⚠️ **Heads-up — the current setup has no rotation story and a single point of catastrophe.** `PHI_ENCRYPTION_KEY` is one Fernet key in an env var. Lose it and **every encrypted column is permanently unrecoverable** — patient names, note contents, revisions. Leak it and all of it is exposed. Before pilot: back that key up somewhere a person can't casually delete, and write down how you'd rotate it (which, with plain Fernet, means re-encrypting every row — so know that cost now).

🧠 **Your call — Fernet-in-app, `pgcrypto`, or KMS envelope encryption?** `docs/tech-stack.md` specified `pgcrypto` and the implementation used app-layer Fernet (it works identically on SQLite, which the test suite needs). That divergence is fine but should be a decision, not an accident. Envelope encryption with a managed KMS is the answer that makes rotation tractable and keeps keys off your servers; it's also more infrastructure than a pilot may warrant. Ask Remedy's DPO what they'll require *before* wider rollout, because retrofitting is much worse than starting there.

### 4.2 Audit logging

- [ ] Audit every PHI access, not just three write paths. Today `audit.record` is called in a handful of places and most reads go unlogged.
- [ ] Log the actor, action, entity, and timestamp for every read of a patient, note, transcript, or audio object.
- [ ] Make audit logs tamper-evident (same append-only trigger pattern as the consent ledger).
- [ ] Build a review interface, or at minimum a documented query — "reviewable" is the actual requirement.
- [ ] Set an audit-log retention period (likely longer than PHI retention).

⚠️ **Heads-up:** "access logs" means *reads*, and reads are the ones developers forget, because nothing visibly breaks when they're missing. The failure only surfaces during an audit or a breach investigation, when the question is "who looked at this patient's record?" and the answer is "we don't know."

### 4.3 Operational security

- [ ] Secrets in a real secret manager, not `.env` files on servers.
- [ ] Dependency scanning in CI (`pip-audit`, `npm audit`) — the mobile scaffold already reports vulnerabilities.
- [ ] Reconsider `passlib` + the `bcrypt==4.0.1` pin. That pin exists to work around a passlib incompatibility, and pinning a security-critical library backwards is a debt you should schedule, not forget. Modern alternative: `argon2-cffi` or `bcrypt` directly.
- [ ] Backup and *tested* restore for Postgres. An untested backup is a hope.
- [ ] Breach response runbook (roadmap Week 5).

### 4.4 Retention enforcement ⚠️

- [ ] Implement the job that actually deletes expired audio. `audio_retention_expires_at` is set on every encounter and **nothing reads it** — retention is currently a column, not a policy. (As of Phase 1.2, `Transcript.retention_expires_at` is in the exact same state — set on every transcript, read by nothing. Two columns now waiting on this one job, not one.)
- [ ] Extend deletion to transcripts (`Transcript.retention_expires_at` already exists — see above) and note revisions, not just audio.
- [ ] Log every deletion to the audit trail.
- [ ] Handle the withdrawal case as an immediate-deletion path.

🧠 **Your call — Celery Beat, a cron job, or bucket lifecycle rules?** Bucket lifecycle is the most reliable for the audio objects themselves (the storage layer enforces it whether your app is running or not) but it can't touch Postgres rows. Celery Beat keeps the logic in your app where it can cascade to transcripts and revisions. Most likely you want both: lifecycle as the backstop, an application job for the derived data. Note the open question in the PRD — the actual retention period is still owned by Legal/Compliance, so keep it configurable, which the scaffold already does.

---

## Phase 5 — Deployment and operations

### 5.1 Deployment 🧠

- [ ] Choose and provision a hosting target.
- [ ] Production `docker-compose` or equivalent; the current one is dev-only (note the shifted host ports 5433/6380/9002, a local-collision workaround).
- [ ] Managed Postgres with automated backups and PITR.
- [ ] Managed Redis, or accept and document the data-loss window.
- [ ] Run migrations as an explicit deploy step, never automatically on boot.
- [ ] Health/readiness endpoints wired to the orchestrator (`/health` exists; it doesn't check DB or Redis).

🧠 **Your call — where does this run, and in which jurisdiction?** Data residency is a real question for Philippine health data, and it may constrain your provider list before performance or cost does. Ask Legal early. Beyond that: a single VM with Docker Compose is the cheapest thing that works and is entirely defensible for a pilot in Remedy's own clinics; managed containers cost more and remove a class of 2 a.m. problems; Kubernetes is almost certainly wrong at this scale. Pick the least infrastructure that meets the compliance bar.

### 5.2 Observability

- [ ] Structured logging with correlation IDs threaded from mobile request through Celery task.
- [ ] Error tracking (Sentry or similar) on both API and mobile.
- [ ] Metrics: pipeline latency per stage, failure rate by stage, queue depth, per-consult cost.
- [ ] Alerts for: pipeline failure rate, stuck encounters, queue depth, upload failure rate.
- [ ] A cost dashboard against the PRD's <$0.10/consult target.

⚠️ **Heads-up — scrub PHI from logs and error reports.** This is the single easiest way to leak clinical data, and it happens by accident: an exception with a transcript in the message, a request body captured by your error tracker, a debug log of a patient name. Configure Sentry's data scrubbing *before* pointing it at production, not after.

📚 **Understand first:** a correlation ID that survives the async boundary is what turns "note generation is sometimes slow" into "note generation is slow for encounters with >20 minutes of audio." Pass it explicitly into the Celery task — it will not propagate on its own.

### 5.3 CI/CD

- [ ] CI: lint (`ruff`), type-check (`mypy`), test on **Postgres** (see 0.5), build the mobile bundle. (`ruff` and `mypy` both run clean locally as of the 2026-08-25 refresh — `apps/api/mypy.ini` now exists — so this item is now "wire the existing green checks into CI," not "get them green.")
- [ ] Migration safety check — fail CI if a migration is missing for a model change.
- [ ] Staging environment with realistic synthetic data (never production PHI).
- [ ] Mobile release pipeline (EAS Build, internal distribution for pilot doctors).

---

## Phase 6 — Pilot instrumentation

Everything the PRD promises to measure needs code before week 0, not after.

- [ ] Edit-burden computation from `NoteRevision` rows (see the 2.6 decision on "minor").
- [ ] Correctly-filed-rate tracking.
- [ ] Post-encounter five-star rating prompt.
- [ ] Documentation-time measurement, comparable to the week-0 paper baseline.
- [ ] Voluntary-use tracking (is this doctor still using it unprompted in week 4?).
- [ ] A weekly manual-review sampling workflow for unsafe-acceptance rate.

⚠️ **Heads-up:** the roadmap's stated mitigation for skipping the vendor bake-off is "watch the edit-burden metric closely from day one of internal alpha." That mitigation only exists if the metric is instrumented *before* alpha. If it isn't, the accepted risk quietly becomes an unmonitored risk — the worst outcome available, because you'll have neither pre-validation nor early detection.

---

## Phase 7 — P1 fast-follows (post go/no-go)

Build only after the Week 6 checkpoint passes.

- [ ] Patient-facing plain-language summary (Filipino, 6th-grade reading level, on request, delivered before the patient leaves).
- [ ] Referral letter drafting.
- [ ] Prior-visit Assessment/Plan auto-injected into the note-generation prompt as labeled historical context.
- [ ] Front-desk check-in queue integration (also the cleanest answer to the 2.5 patient-search problem).
- [ ] Dermatology quick-entry pad for Objective findings.

📚 **Understand first:** the PRD's P2 list is a set of things not to architect *out*. When you make choices in Phases 1–5, occasionally sanity-check them against per-doctor templates, medical certificates, multi-tenancy, and EMR integration. You don't build for them — you just avoid decisions that would make them require a rewrite. The `NoteGenerator` interface is a good example of this done right: swapping models is a config flag because someone thought about it in advance.

---

## Cross-cutting: what to write down as you go

Create `docs/decisions/` and record every 🧠 above as a short entry: the decision, the options considered, why you chose, and what would change your mind. Four sentences is enough.

Two reasons this matters more than it seems. First, in eight weeks you will not remember why you picked chunked-upload-by-hand over presigned URLs, and neither will anyone reviewing it. Second — and this is the part that's specific to you — writing the "what would change my mind" line is what turns a decision you *made* into a decision you *understand*. If you can't fill that line in, you probably haven't finished thinking about it yet.

Keep `docs/tech-stack.md` honest as reality diverges from it. It already has one divergence worth fixing: it specifies `pgcrypto` and the code uses app-layer Fernet. A stack doc that quietly stops matching the code is worse than no stack doc, because people trust it.

---

## Suggested sequencing

Given a lean team and the roadmap's 4–8 week MVP target:

| Order | Work | Why here |
|---|---|---|
| 1 | Phase 0 | These are false claims in the codebase. Fix before building on them. |
| 2 | Phase 1.1–1.2 | Upload + transcript persistence unblock everything downstream. |
| 3 | Phase 2.1–2.2 | Recording is the longest-lead, highest-risk work. Start early, fail early. |
| 4 | Phase 1.3–1.5 | Real ASR + note-gen, now that you have real audio to feed them — plus the failure handling that makes them safe to run unattended. |
| 5 | Phase 2.3–2.6 | Consent, queue, identity, review/sign → **internal alpha**. |
| 6 | Phase 6 | Instrument *before* alpha, not after. |
| 7 | Phase 3 | Grounding UI. |
| 8 | Phase 4–5 | Security hardening + deployment before real patients. |
| 9 | → Go/no-go | Then Phase 7. |

Phase 4 sits late in this list but has a hard constraint: **no real patient data touches this system until 4.1, 4.2, and 4.4 are done**, regardless of what else is finished. Alpha on synthetic or fully-consented internal recordings is fine before then. Real consultations are not.

And the standing blocker from the roadmap: consent-gated recording cannot go live to real patients until Philippine counsel clears the RA 4200 flow. Build it, test it, keep it dark until Legal signs off.
