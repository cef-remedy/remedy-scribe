# Remedy Scribe — Production Implementation Checklist

**Purpose:** everything between the current scaffold and a system that can legally and safely record real consultations in a Remedy clinic.
**Companion docs:** `remedy-scribe-prd.md` (what/why) · `remedy-scribe-roadmap.md` (when) · `docs/tech-stack.md` (with what)

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

What actually runs today, confirmed by executing it — not by reading the README:

**Real and tested:** the data model (clinicians, patients, encounters, consent ledger, notes, revisions, audit log); Alembic migrations against live Postgres; the four-state note lifecycle with skip-prevention; patient fuzzy-match + name-and-birthdate dedup; JWT + TOTP login; the consent ledger's append-only Postgres trigger (verified by hand: `UPDATE` and `DELETE` both raise). 9 passing tests. A live server driven end-to-end with curl through login → patient match → encounter → consent.

**Wired but hollow:** the Celery chain exists and is idempotent, but `transcribe_encounter` throws away its own output (`_ = segments`) and `generate_note` always calls the model with `transcript=[]`. Both provider interfaces (`ASRProvider`, `NoteGenerator`) are real; both implementations raise `NotImplementedError`.

**Absent entirely:** any upload path (no endpoint, no S3 client — `boto3` is in `requirements.txt` and imported nowhere); transcript persistence; the grounding UI's data; retention enforcement; consent *enforcement*; RBAC enforcement; and the entire mobile client (72 graph nodes under `apps/mobile`, every one of them `App.tsx` boilerplate or config).

That last line is the honest headline: **the doctor-facing app does not exist yet.** Everything below is sequenced around that.

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

- [ ] Convert `Encounter.pipeline_status` from a free-form `String(32)` to a proper enum, the way `Note.status` already is. It currently accepts any string, and the codebase writes at least five different values across two files.
- [ ] Move `confirm_upload`'s `audio_object_key` from a query parameter into a Pydantic request body.
- [ ] Add a `CHECK` constraint or enum for `ConsentLedgerEntry.event` (`given|declined|withdrawn`).

📚 **Understand first:** why enums-at-the-DB-layer matter more here than in a typical CRUD app. Both `Note.status` and the consent ledger are *legal* records. "The database physically cannot hold an invalid value" is a much stronger statement to an auditor than "our code only ever writes valid values." That's the same reasoning behind the append-only trigger — push the guarantee as far down the stack as it will go.

### 0.5 Close the test-vs-production divergence ⚠️

- [ ] Add a Postgres-backed test path (testcontainers, or a CI service container) for the tests that depend on Postgres-specific behavior.
- [ ] Write a test proving the consent ledger rejects `UPDATE` and `DELETE`.

⚠️ **Heads-up — this one is sharp.** The test suite runs on SQLite via `Base.metadata.create_all()`. The append-only consent trigger lives in an Alembic migration. **Migrations never run in the test suite, so the trigger is never exercised by a single test.** I verified it manually with `psql`, which is why I know it works — but manual verification is not a regression test. Someone could drop that migration tomorrow and every test would still pass. Any guarantee implemented in SQL rather than Python is currently untested by construction.

📚 **Understand first:** "test against what you deploy." SQLite-for-speed is a common and often reasonable trade, but it silently voids every Postgres-specific guarantee: triggers, `pgcrypto`, native enums, `CHECK` constraints with Postgres semantics, concurrent-transaction behavior. Know exactly which of your guarantees fall in that blind spot, and cover those on real Postgres.

---

## Phase 1 — Make the pipeline real

Goal: audio recorded on a device ends up as a structured note in Postgres, with no human in the loop.

### 1.1 Upload path 🧠 📚

- [ ] Implement an S3/MinIO client module (`app/services/storage.py`) — `boto3` is already a declared dependency and currently unused.
- [ ] Implement chunked, resumable upload. Endpoints, roughly: `POST /encounters/{id}/upload/init` → `PUT /encounters/{id}/upload/chunk/{n}` → `POST /encounters/{id}/upload/complete`.
- [ ] Persist per-chunk state so a resumed upload skips what already landed.
- [ ] Enforce the idempotency key across the whole flow, not just encounter creation.
- [ ] Server-side encryption at rest on the bucket, plus a lifecycle policy keyed to `AUDIO_RETENTION_DAYS`.

🧠 **Your call — build the upload protocol or adopt one?** Three real options:
- **S3 multipart with presigned URLs.** The device uploads directly to object storage; your API only mints URLs and gets a completion callback. Cheapest to run, least bandwidth through your server, natively resumable. Cost: presigned-URL scoping is easy to get subtly wrong, and your API no longer sees the bytes (so it can't enforce anything about them).
- **[tus.io](https://tus.io) resumable protocol.** A real spec with mature client and server implementations, designed for exactly this. Cost: another moving part to run and understand.
- **Roll your own chunk endpoints.** Total control, matches the PRD's wording directly, and you'll understand every failure mode because you wrote them. Cost: you will reimplement bugs the other two already fixed — partial-chunk corruption, concurrent resume, orphaned uploads.

For learning value, rolling your own once is genuinely instructive. For a 4–8 week clinical MVP, presigned multipart is the pragmatic answer. If you roll your own, at minimum handle: chunk checksums, out-of-order arrival, and an orphan-upload reaper.

📚 **Understand first:** why idempotency keys exist at all. A phone on clinic wifi will retry a request whose response it never saw. Without a key, "retry" and "second consultation" are indistinguishable to your server, and you get duplicate notes on the same patient — a clinical-safety bug, not just a data bug. Trace the key's path through `encounters.py` and convince yourself where a duplicate could still slip through today.

⚠️ **Heads-up:** local audio must only be deleted after the server confirms *both* receipt and that note generation has begun (P0-2). Deleting on upload-complete alone means a server-side pipeline crash loses the consultation permanently. The confirmation the device waits for should be about the pipeline, not the bytes.

### 1.2 Transcript persistence 🧠

- [ ] Add a transcript model/table (or object-storage document) holding: full text, per-word timings, per-word confidence, and speaker labels.
- [ ] Make `transcribe_encounter` actually persist its output. It currently computes `segments` and discards them with `_ = segments`.
- [ ] Make `generate_note` load the persisted transcript instead of passing `transcript=[]`.

🧠 **Your call — where does the transcript live?** Options:
- **Postgres `JSONB` column.** Queryable, transactional with the note, encrypted with your existing `EncryptedString` approach if you wrap it. Cost: word-level data for a 20-minute consult is large; you'll be loading megabytes to render one note.
- **Row-per-word table.** Precise, indexable by time, ideal for the grounding UI's "play from here." Cost: hundreds of thousands of rows per clinic-week and a heavier write path.
- **Object storage, like the audio.** Cheap, unlimited size. Cost: not queryable, another fetch on the read path, and a second place PHI lives that retention must remember to purge.

This choice largely determines how hard Phase 3 (grounding UI) is, so think about that requirement now rather than after. My instinct is `JSONB` for the MVP with the *sentence* as the addressable unit, because it keeps one transactional home for one note's data — but if you want word-precision audio seeking, the row-per-word table stops being overkill.

⚠️ **Heads-up:** the transcript is PHI, arguably more sensitive than the note (it's verbatim, including things the doctor chose not to record). Whatever you pick, it needs the same encryption, the same access logging, and the same retention clock as the audio. A retention job that purges audio and leaves transcripts is not a retention policy.

### 1.3 Real ASR integration ⚠️ 🧠

- [ ] Implement `ElevenLabsScribeProvider.transcribe` — stream the object from storage, POST to Scribe v2 with diarization enabled.
- [ ] Handle rate limits, timeouts, and partial failures with Celery retries (already scaffolded via `self.retry`).
- [ ] Record which ASR provider and model version produced each transcript.
- [ ] **Fix `_parse_response`** — see the heads-up below.

⚠️ **Heads-up — there is a real bug in the stub I wrote.** `_parse_response` groups every word by speaker across the entire recording, producing one giant segment per speaker. That destroys turn order: you get "everything the doctor said" then "everything the patient said," instead of the actual back-and-forth. A note generated from that will mangle who reported which symptom. Segments must be *turns* — split when the speaker label changes. Worth reading that function and seeing the bug yourself before fixing it; it's a good example of code that looks reasonable and is semantically wrong.

⚠️ **Heads-up — diarization gives you anonymous labels.** Scribe returns `speaker_0`, `speaker_1` — not "doctor" and "patient." Mapping them is your problem, and getting it backwards inverts Subjective content (patient's reported symptoms become the doctor's words). Heuristics that actually work: the doctor speaks first (they start the recording), the doctor speaks the consent script, the doctor has more total speech time. None are reliable alone. Consider having the consent script itself serve as the doctor's voice fingerprint, since you know who reads it.

🧠 **Your call — how do you validate ASR quality with no bake-off?** The roadmap explicitly dropped the vendor bake-off and accepted this risk, making internal alpha the first real test. So decide now what you'll measure and how: a small set of consented recordings hand-transcribed as ground truth? Clinically-weighted entity error rate on drug names and doses (as the PRD's success metrics suggest)? Doctor-reported "did you have to fix a name/dose" flag on each note? Pick something cheap and start collecting from day one of alpha — the risk register says this surfaces via edit burden, and edit burden is only measurable if you instrumented it before the first note.

### 1.4 Real note generation ⚠️ 🧠

- [ ] Implement `LunaNoteGenerator.generate` — single fused call (P0-4), APSO section order, hedged language, silence/low-confidence suppression.
- [ ] Implement `HaikuNoteGenerator.generate` as the configured fallback.
- [ ] Use structured output (JSON schema / tool call), not free-text parsing.
- [ ] Pass word-level confidence into the prompt in a form the model can act on.
- [ ] Store the prompt version alongside each generated note.
- [ ] Add a golden-transcript test suite: fixed transcript in, assertions on the note out.

⚠️ **Heads-up:** "generation is suppressed over silent or low-confidence windows" (P0-4) will not happen just because your system prompt says so. Models are strongly biased toward producing fluent, complete-looking clinical text. If you hand it a transcript with a garbled 30-second stretch, it will smooth over the gap plausibly and you will not be able to tell. Make suppression *mechanical* where you can: mark low-confidence spans in the input explicitly (e.g. `[INAUDIBLE 0.31]`), and validate the output for invented content rather than trusting instruction-following.

⚠️ **Heads-up:** storing the prompt version per note matters more than it sounds. When edit burden jumps in week 3, the first question is "did we change the prompt?" — and without a version stamped on each row, that question is unanswerable after the fact.

🧠 **Your call — how do you get trustworthy source spans?** P0-4 requires every generated line to trace back to its transcript passage, and P0-7 builds a UI on that. But **an LLM asked to emit character offsets will produce confident, wrong numbers** — it cannot count characters reliably. Options:
- Have the model **quote** the exact supporting passage verbatim, then string-search the transcript server-side to compute real offsets. Slower, more tokens, but the offsets are ground truth.
- Give each transcript sentence a stable **ID** in the prompt and have the model cite IDs. Cheap, robust, coarser granularity.
- Ask for offsets directly. Cheapest, and I'd expect it to be wrong often enough to break the feature.

I'd take sentence IDs: it's the only option that's both cheap and verifiable, and sentence-level grounding is almost certainly enough for a doctor scanning for "where did this come from?"

📚 **Understand first:** the difference between a model *citing* and a model *appearing to cite*. Any output can contain a plausible-looking reference. Only a reference you independently verified against the source is evidence. This distinction is the whole reason the grounding UI is a P0 requirement rather than a nice-to-have — it's the doctor's mechanism for catching exactly this failure.

### 1.5 Pipeline failure handling

- [ ] Give the doctor a specific, actionable error state per failure mode (upload failed / transcription failed / generation failed) — the PRD's edge case explicitly rejects "a silent gap in the record."
- [ ] Add a dead-letter path: after max retries, mark the encounter failed and surface it in the app.
- [ ] Never leave an encounter stuck in an intermediate `pipeline_status` with nothing watching it.
- [ ] Add a "regenerate note" action for transient failures.

📚 **Understand first:** a queue system's real hard part isn't throughput, it's *stuck work*. Every async pipeline eventually has jobs that neither succeeded nor failed loudly. Decide now how you'll notice: a periodic sweep for encounters older than N minutes in a non-terminal state is the usual answer, and it's much easier to add before you have production data than after.

---

## Phase 2 — Build the mobile app

This is the largest phase and currently 0% done. Everything above is invisible to a doctor without it.

### 2.1 App foundation

- [ ] Navigation (`expo-router` or React Navigation) — today `App.tsx` is a single static screen.
- [ ] Generate a typed API client from FastAPI's OpenAPI schema (`openapi-typescript-codegen` or `orval`) rather than hand-writing fetch calls. This is the payoff for the Python-backend/TypeScript-client split described in `docs/tech-stack.md`.
- [ ] Auth flow: login → TOTP → secure token storage → biometric unlock on resume.
- [ ] Error/offline UI primitives you'll reuse everywhere.
- [ ] Build a real dev client (`npx expo run:android`) — Expo Go cannot host the native modules you need.

⚠️ **Heads-up:** you need a working Android/iOS toolchain for this phase, and it is a genuine time sink the first time. Budget real hours for Android Studio, SDK versions, and (for iOS) a Mac plus a paid Apple developer account. This is the most common place a mobile-first plan slips, and it slips for reasons that have nothing to do with your code.

### 2.2 Recording — the hard part 📚 ⚠️ 🧠

- [ ] One-tap record with a persistent, always-visible recording indicator (P0-1).
- [ ] Background capture that survives app backgrounding, screen lock, and incoming calls (P0-2).
- [ ] Android: foreground service with an ongoing notification.
- [ ] iOS: `UIBackgroundModes: audio` plus correct `AVAudioSession` category.
- [ ] Encrypt audio on-device *before* it touches disk, key sealed in Keychain/Keystore.
- [ ] Handle interruptions: pause/resume on phone call, save partial audio on crash.
- [ ] Write audio in chunks as you go, never buffering a whole consult in memory.

📚 **Understand first:** mobile OSes treat background microphone access as adversarial by default, and reasonably so. Both platforms will kill or silence a backgrounded recorder unless you declare intent through a specific mechanism — a foreground service on Android, a background mode on iOS — and both require user-visible indication. This is precisely why `docs/tech-stack.md` rules out a PWA: browsers give you no such mechanism at all. Read the platform docs for background audio before writing this; it's the difference between a day and a week.

⚠️ **Heads-up:** Apple review will ask why a medical app records in the background. Have the clinical justification written down before you submit, and expect the consent flow to be the thing that satisfies them.

⚠️ **Heads-up:** a 30-minute consultation is a big file, and battery/thermal behavior over a full clinic day is a real constraint nobody models in advance. Test an actual 8-hour day of intermittent recording on a real mid-range Android phone — the kind a doctor actually carries, not a flagship. The PRD lists "what devices do doctors actually carry" as an open question; answer it before you tune bitrate.

🧠 **Your call — audio format and bitrate.** You're trading ASR accuracy against file size against upload time on clinic wifi. Speech at 16 kHz mono in a compressed format (AAC/Opus) is usually plenty for ASR and dramatically smaller than uncompressed WAV. But verify against Scribe's documented input expectations before committing, and test whether aggressive compression measurably hurts Taglish accuracy — code-switched speech may be less robust to compression artifacts than monolingual English.

### 2.3 Consent flow (P0-1)

- [ ] Consent screen presenting the script in Filipino and English, blocking recording until resolved.
- [ ] Capture the participant roster before recording starts.
- [ ] Record the spoken consent exchange as the first segment of the audio file.
- [ ] Handle decline gracefully — the app must remain fully usable without recording (explicit PRD edge case).
- [ ] Mid-visit re-consent: pause recording, capture new roster, log a new ledger entry, resume.
- [ ] Withdrawal action, available at any time, that reaches the server.

⚠️ **Heads-up:** withdrawal currently has *no* server-side effect — no pipeline abort, no deletion. P0-1 says processing stops and audio is queued for deletion "without undue delay." Implementing that means revoking a Celery task that may already be mid-flight, which is genuinely awkward: you can't reliably kill a running task, so the pattern is a consent check at each stage boundary plus a cleanup job. Design for "stops at the next checkpoint," not "stops instantly," and make sure that's what you tell Legal it does.

### 2.4 Offline queue (P0-2)

- [ ] SQLite-backed durable queue (`expo-sqlite`) surviving app kill and device restart.
- [ ] Visible, persistent queue status — nothing may fail silently (explicit PRD requirement).
- [ ] Background upload with exponential backoff.
- [ ] Generate the idempotency key on-device at recording start, and persist it before the first byte is uploaded.
- [ ] Delete local audio only after the server confirms receipt *and* pipeline start.
- [ ] Handle the device-full case.

📚 **Understand first:** this is a write-ahead log, the same pattern databases use for durability. The invariant is that the *record of intent* is committed to durable storage before the risky operation begins, so a crash at any point leaves you able to reconstruct what should happen. If you generate the idempotency key in memory and crash before persisting it, the retry generates a new key and you get a duplicate — which is exactly the bug the key exists to prevent.

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

- [ ] Implement the job that actually deletes expired audio. `audio_retention_expires_at` is set on every encounter and **nothing reads it** — retention is currently a column, not a policy.
- [ ] Extend deletion to transcripts and note revisions, not just audio.
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

- [ ] CI: lint (`ruff`), type-check (`mypy`), test on **Postgres** (see 0.5), build the mobile bundle.
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
| 4 | Phase 1.3–1.4 | Real ASR + note-gen, now that you have real audio to feed them. |
| 5 | Phase 2.3–2.6 | Consent, queue, identity, review/sign → **internal alpha**. |
| 6 | Phase 6 | Instrument *before* alpha, not after. |
| 7 | Phase 3 | Grounding UI. |
| 8 | Phase 4–5 | Security hardening + deployment before real patients. |
| 9 | → Go/no-go | Then Phase 7. |

Phase 4 sits late in this list but has a hard constraint: **no real patient data touches this system until 4.1, 4.2, and 4.4 are done**, regardless of what else is finished. Alpha on synthetic or fully-consented internal recordings is fine before then. Real consultations are not.

And the standing blocker from the roadmap: consent-gated recording cannot go live to real patients until Philippine counsel clears the RA 4200 flow. Build it, test it, keep it dark until Legal signs off.
