# Tech Stack Decision — Remedy Scribe

**Status:** Decided · **Date:** August 18, 2026 · **Based on:** `remedy-scribe-prd.md`, `remedy-scribe-roadmap.md`

This is an engineering decision record, not a menu of options. Each choice is
justified against a specific PRD requirement. Where the PRD is silent, the
choice optimizes for the stated constraint: lean team, 4–8 week MVP.

---

## 1. Client — browser web app (PWA-shaped) on a clinic laptop + TypeScript

> **Superseded 2026-08-27 (decision 0024).** This section previously read
> "React Native (Expo, custom dev client)" and rejected a web/PWA client.
> Both of its stated reasons were specific to a *phone*, and the phone
> premise turned out to be wrong. The original text is preserved at the
> bottom of this section, because the reasoning was sound given what was
> assumed at the time — it was the assumption that failed, not the logic.

**Decided:** a browser-based web app — service worker offline shell,
IndexedDB write-ahead queue — running in Chrome or Edge on a clinic
laptop. `apps/mobile/`'s Expo scaffold is retired.

**What changed:** the PRD listed *"What devices do doctors actually carry,
and does a phone fit the physical workflow?"* as an Open Question owned by
Design/Engineering. On 2026-08-27 the supervisor answered it: consultations
run primarily on **laptops**. The rejection of a web client rested on
"mobile browsers suspend microphone capture on background/lock" — true of
mobile browsers, and irrelevant on a laptop.

**Measured, not assumed** (`docs/experiments/audio-capture-harness.html`,
one 29-minute run on the real hardware — Windows, Chrome 151):

- Audio missing during 131 s of backgrounded time, across 9 hidden
  windows: **0.05 s**.
- Page timers did not throttle (1737/1743 ticks, identical to a Worker),
  because Chrome exempts tabs holding active capture with a live audio
  graph. So encoding does **not** need to move into a Worker here.
- All measurable loss in the run — 6.5 s of 7.7 s total — came from a
  single **system-sleep** event. That is the one real risk, and it favours
  no architecture: lid close is OS power policy, which neither a web page
  nor Electron can veto.

**Consequences for P0-2:** its interruption clause re-scopes to the laptop
form factor. Backgrounding and screen lock are measured as satisfied;
lid close/sleep is not, and is unsatisfiable in software — mitigated by
Windows power policy ("When I close the lid → Do nothing") plus chunked
IndexedDB writes so a suspend truncates rather than destroys a recording.
Whoever signs off on P0-2 needs that stated plainly.

Key libraries and mechanisms:
- `getUserMedia` + `MediaRecorder` for capture, with the mime type
  **feature-detected** via `MediaRecorder.isTypeSupported()` rather than
  hardcoded — Chrome/Edge/Firefox give WebM/Opus, Safari was MP4/AAC-only
  before 18.4. Groq Whisper accepts both.
- An `AudioContext` + `AudioWorklet` alongside it for level metering and
  the persistent recording indicator (P0-1) — and, as the harness showed,
  keeping a live audio graph is part of what earns the throttling
  exemption.
- Web Crypto (`AES-GCM`, non-extractable `CryptoKey`) for on-device
  encryption before any network activity (P0-2). Note the honest gap
  versus the retired mobile plan: a browser cannot seal the key in a
  hardware keystore the way Keychain/Android Keystore could. If Legal
  requires hardware-sealed key custody, decision 0024's option (b) —
  an Electron wrapper — becomes necessary.
- IndexedDB as the local write-ahead queue for upload state, chunk
  progress, and idempotency keys, replacing `expo-sqlite`. Same
  write-ahead-log invariant, different store.
- A **screen wake lock re-acquired on every `visibilitychange → visible`**.
  The browser auto-releases it on hide and never restores it; the first
  harness run lost it 35 s in and ran unguarded for 28 minutes. This is
  mandatory, not a nicety.
- WebAuthn (Windows Hello / Touch ID) for biometric unlock — a better
  cross-platform story than the mobile plan had.
- Resumable chunked upload straight to the existing presigned S3
  multipart endpoints from Phase 1.1 (§2) — the browser `PUT`s to S3
  directly, no new backend work.

**Token storage** amends decision 0006 rather than reversing it: the shape
survives (short-lived in-memory access token + persisted revocable refresh
token + server-side revocation), but the mechanism changes from
`expo-secure-store` to an httpOnly/Secure/SameSite cookie — stronger
against XSS, weaker on hardware sealing.

<details>
<summary><b>Original text, superseded (kept for the reasoning)</b></summary>

> **Decided in discussion:** cross-platform mobile, not a web/PWA client.
>
> The web/PWA alternative was ruled out because P0-2 requires recording to
> "survive interruption (incoming call, app backgrounded, device locked)
> without data loss" — mobile browsers suspend microphone capture on
> background/lock, which directly conflicts with that requirement. A phone app
> is also the framing throughout the PRD's user stories ("one tap," "the
> doctor's device").
>
> **React Native over two native codebases:** a lean team building an MVP in
> 4–8 weeks cannot maintain separate Swift and Kotlin apps. One TypeScript
> codebase covers iOS + Android.
>
> **Expo with a custom dev client (prebuild/CNG), not Expo managed workflow:**
> background audio capture needs native modules (background task registration,
> foreground service on Android, `AVAudioSession` background mode on iOS) that
> plain Expo managed workflow doesn't expose. A custom dev client keeps Expo's
> tooling (EAS Build, OTA updates for JS-only fixes) while allowing native
> modules — pure Expo Go would block P0-2; pure bare RN CLI would throw away
> EAS/OTA for no benefit here.
>
> Note what this branch of complexity existed for: it was *entirely* downstream
> of needing background audio on a phone. With the laptop form factor it
> disappears rather than being solved.

</details>

## 2. Backend — Python + FastAPI

**User preference, and a legitimate fit:** the backend's job is mostly I/O
orchestration (ASR call → note-gen call → state transitions), not heavy
compute, so language choice is about ecosystem fit, not performance. Python
wins here specifically because:
- Every vendor in the pipeline (ElevenLabs, OpenAI-compatible note-gen,
  Anthropic fallback) ships a first-class Python SDK.
- FastAPI generates an OpenAPI schema for free, which the RN client uses to
  generate a typed API client (`openapi-typescript-codegen` or `orval`) —
  this recovers most of the type-sharing benefit a same-language stack would
  have given, without forcing Python on the mobile side.
- Pydantic models double as the validation layer for the note state machine
  (§5) and the consent ledger schema (§6) — both need strict shape
  enforcement, not just typing.

**Not Node/NestJS:** would share a language with the client, but the AI-SDK
ecosystem argument is one-sided enough (and the user's own preference)
to settle it in Python's favor.

## 3. Database — PostgreSQL

Relational by nature: patients, encounters, consent-ledger entries, notes,
note revisions, and audit/access logs all have real foreign-key
relationships (P0-6 dedup on name+birthdate, P0-5's four-state note
lifecycle, P0-8's access/change logs). Postgres specifically for:
- Row-level constraints and enum types to make the note state machine
  (`generated → filed → authenticated → signed`) enforceable at the DB
  layer, not just in application code — "no state skippable" (P0-5)
  shouldn't rely on the API being the only caller.
- `pgcrypto` for field-level encryption of PHI columns (transcript text,
  patient name/birthdate) — satisfies P0-8 encryption-at-rest without a
  separate KMS-backed service for the MVP.
- Native support for append-only tables (via `REVOKE UPDATE, DELETE` on the
  consent-ledger table + a role that only has `INSERT`) — the cheapest way
  to get the "immutable, append-only consent ledger" (P0-1) that's actually
  enforced, not just documented as a convention.

## 4. Object storage — S3-compatible (MinIO locally, S3-compatible bucket in prod)

Audio files and generated transcripts are blobs, not rows. Encrypted at rest
via server-side encryption (SSE), separate from the Postgres-level
`pgcrypto` encryption of structured PHI. MinIO in local/dev docker-compose
gives a bit-for-bit-compatible API to whatever S3-compatible bucket is
chosen in prod, so no code branches on environment.

Retention (P0-2 "local audio deleted only after server confirms receipt and
note generation has begun"; Compliance's "audio retention duration is
configurable, not hardcoded") is a lifecycle policy on the bucket plus an
explicit `retention_expires_at` column per audio object — not a hardcoded
TTL in code.

## 5. Async pipeline — Celery + Redis

Transcription (ElevenLabs Scribe v2) and note generation (GPT-5.6 Luna,
falling back to Claude Haiku 4.5) are both slow, external, and retryable —
exactly Celery's use case, and it's the standard pairing with a Python
backend. Redis serves double duty as the Celery broker and as a cache for
the patient fuzzy-match lookup (P0-6).

Concretely, one encounter's pipeline is a Celery chain:
`upload_finalized → transcribe → generate_note → notify_client`, with the
idempotency key from P0-2 ("uploads are resumable and chunked, with an
idempotency key that prevents duplicate notes from a retried upload") stored
as a unique constraint on the encounter's pipeline-run row — a retried
upload chunk resolves to the same run instead of enqueuing a second one.

The note-generation step is a small provider interface
(`NoteGenerator.generate(transcript) -> Note`) with two implementations
(Luna, Haiku), selected by a config flag — this is what lets the PRD's
"Claude Haiku 4.5 remains available as a configured fallback" (P0-4) be a
one-line config change instead of a code change, per the roadmap's own
mitigation for the no-bake-off risk.

## 6. Auth & security baseline (P0-8)

- **AuthN:** JWT access/refresh tokens issued by the FastAPI backend;
  password + TOTP for MFA (`pyotp` + an authenticator app) rather than
  SMS OTP, since Remedy is not a telco and SMS delivery in the Philippines
  is a needless external dependency and cost.
- **AuthZ:** role field on the clinician record (`doctor`, `compliance`,
  `admin`) checked via a FastAPI dependency on every route — need-to-know
  RBAC per P0-8, not a separate authorization service for v1.
- **Transit:** TLS everywhere (terminated at the load balancer / reverse
  proxy in front of FastAPI).
- **Audit trail:** a single `audit_log` table (actor, action, entity,
  before/after diff, timestamp) written to by a FastAPI middleware, not
  scattered per-endpoint logging — covers "access and change logs retained
  and reviewable" (P0-8) and the signing audit trail (P0-5: doctor identity,
  PRC license number, timestamp).

## 7. Vendor integrations (fixed by the PRD, not re-decided here)

- **ASR:** ElevenLabs Scribe v2, diarization on, called from a Celery task.
  Contingent on the Legal BAA/DPA question in Open Questions — the provider
  interface in §5 is what makes swapping to Speechmatics a config change if
  that clears negatively.
- **Note generation:** GPT-5.6 Luna primary, Claude Haiku 4.5 fallback —
  same provider-interface pattern.

## 8. Repo layout

```
remedy-note-taker/
  apps/
    mobile/        # React Native (Expo, custom dev client), TypeScript
    api/           # FastAPI, Python
  infra/
    docker-compose.yml   # postgres, redis, minio, api — local dev
  docs/
    tech-stack.md  # this file
    remedy-scribe-prd.md
    remedy-scribe-roadmap.md
```

A monorepo (not separate repos for mobile/api) because the two halves ship
together at every milestone in the roadmap (Week 1 core pipeline, Week 2
identity + review + signing) — separate repos would just add coordination
overhead for a lean team with no other consumers of either half.

## 9. What's deliberately deferred

- **KMS-backed envelope encryption** for PHI columns — `pgcrypto` with an
  app-managed key is enough for the pilot; revisit if Remedy's DPO requires
  key rotation/HSM-backed keys before wider rollout.
- **A managed message queue (SQS/RabbitMQ) in place of Redis** — Redis
  covers both the Celery broker and the cache need at pilot scale; revisit
  if throughput or delivery-guarantee needs grow post-pilot.
- **Multi-region / multi-tenant infra** — explicitly a non-goal (PRD
  Non-Goals: "Multi-tenant configurability").
