# Decisions log

One file per decision, in the format `docs/implementation-checklist.md` asks
for: the decision, the options considered, why we chose what we chose, and
what would change our mind. Four sentences is enough — this is a log, not a
design doc.

Every 🧠 **Your call** in the checklist gets an entry here. Some entries are
mine (an implementation-level choice made while executing a checklist item —
still worth a record so we don't re-litigate it or forget the reasoning);
those are marked **Decided by:** implementation. Anything that's actually the
product/legal/risk fork the checklist flags for you is marked **STATUS: OPEN
— your call** and left unfilled until you decide it.

| # | Phase | Decision | Status |
|---|---|---|---|
| [0001](0001-consent-ledger-read-model.md) | 0.1 | How `assert_consent_valid` reads "current" consent state from an append-only ledger | Decided (implementation) |
| [0002](0002-consent-violation-is-terminal-not-retried.md) | 0.1 | What the pipeline does when consent is invalid at task time | Decided (implementation) |
| [0003](0003-mid-visit-reconsent-detection.md) | 0.1 / 2.3 | Manual flag vs. diarization-based detection for mid-visit re-consent | Closed 2026-08-27 — **by elimination**: decision 0018 removed diarization entirely, so manual flagging is the only implementable option |
| [0004](0004-note-read-access-scope.md) | 0.2 | Note reads: authoring clinician only, or any clinician in the clinic | Decided (implementation) |
| [0005](0005-rbac-role-policy-and-audit-log-endpoint.md) | 0.2 | Per-route role policy (`doctor` vs `admin` for clinical writes); adding a minimal audit-log endpoint early | Decided (implementation) |
| [0006](0006-mobile-token-storage-and-revocation.md) | 0.3 | Where the token lives on the device; lost-phone revocation | Decided (user) |
| [0007](0007-refresh-token-lifetimes-and-reuse-detection.md) | 0.3 | Access/refresh token lifetimes; what counts as reuse vs. plain revocation | Decided (implementation) |
| [0008](0008-login-rate-limiting-backed-by-db-not-redis.md) | 0.3 | Login rate limit/lockout backed by a DB table instead of Redis | Decided (implementation) |
| [0009](0009-mfa-enrollment-bootstrap-scope.md) | 0.3 | MFA self-service enrollment scoped to first-time setup only | Decided (implementation) — **gap flagged for pilot** |
| [0010](0010-enum-check-constraints-needed-explicit-flags.md) | 0.4 | `Note.status` had no real DB constraint; enum CHECK constraints need explicit `values_callable` too | Decided (implementation) — **bug found empirically** |
| [0011](0011-encounter-pipeline-status-enum-membership.md) | 0.4 | `EncounterPipelineStatus` scoped to today's 5 values, not Phase 1.5's future ones | Decided (implementation) |
| [0012](0012-postgres-test-path-testcontainers-and-subprocess-migrations.md) | 0.5 | testcontainers over a CI service container; migrations run as a real subprocess, not in-process | Decided (implementation) |
| [0013](0013-presigned-multipart-upload-design.md) | 1.1 | Presigned S3 multipart protocol (user's call); server-owned object key; S3 `ListParts` as per-chunk state | Decided (user + implementation) |
| [0014](0014-bucket-lifecycle-rule-shape-and-minio-limitation.md) | 1.1 | Lifecycle rule must combine both actions in one rule; MinIO silently drops the abort action | Decided (implementation) — **bug found empirically** |
| [0015](0015-startup-bucket-provisioning-off-in-tests.md) | 1.1 | Short boto3 timeouts; bucket provisioning disabled in tests | Decided (implementation) — **perf bug found empirically** |
| [0016](0016-transcript-storage-shape.md) | 1.2 | Encrypted JSON blob (user's call); segment-level, not sentence-level, granularity | Decided (user + implementation) |
| [0017](0017-transcript-scope-boundary-with-phase-1.3.md) | 1.2 | `asr_provider` now, `asr_model_version` deferred to 1.3; retention clock added now | Decided (implementation) |
| [0018](0018-groq-whisper-instead-of-elevenlabs-scribe.md) | 1.3 | ASR vendor: Groq-hosted Whisper large-v3, not the PRD's named ElevenLabs Scribe v2 — **diarization capability lost** | Decided (user) |
| [0019](0019-asr-quality-validation-plan.md) | 1.3 | ASR quality validation: two-person test audio, expectations set correctly (no diarization to find) | Decided (user) |
| [0020](0020-mypy-baseline-and-real-httpx-bug.md) | cross-cutting | First-ever mypy run found a real httpx crash and an LSP violation | Decided (implementation) — **real bug found empirically** |
| [0021](0021-haiku-only-luna-dropped.md) | planning update, ahead of 1.4 | Note generation: Haiku only, Luna dropped entirely — **loses P0-4's configured-fallback risk mitigation** | Decided (user) |
| [0022](0022-source-span-citation-design.md) | 1.4 | Source spans cite persisted segment IDs, not model-emitted offsets or timestamps; citations verified, not trusted | Decided (implementation) |
| [0023](0023-pipeline-failure-handling-design.md) | 1.5 | Two failure states, not three (no `upload_failed`); dead-lettering and the stuck-sweep are separate mechanisms; `/retry` re-runs only the failed stage | Decided (implementation) |
| [0024](0024-web-client-on-laptop-not-mobile.md) | 2.1 | Client is a browser web app on a clinic laptop, not Expo mobile — **supersedes tech-stack.md §1**; backgrounding measured safe, lid-close sleep is the one real gap | Decided (user + measurement) |
| [0025](0025-audio-format-mono-opus-32kbps.md) | 2.1 / 2.2 | Audio capture: mono Opus at 32 kbps — and the settings are verified against the device, not trusted | Decided (user) |
| [0026](0026-recorder-design.md) | 2.2 | Recorder: consent gate reads the server and fails closed (incl. offline); audio gaps recorded rather than hidden; 5s chunks ≠ 5 MB S3 parts; crypto-shred is per-device | Decided (implementation) |
| [0027](0027-consent-flow-ordering-and-withdrawal.md) | 2.3 | Consent ordering forced by P0-1 (nothing captured before consent, so the *asking* is not recorded); withdrawal's three ordered actions; script text is a placeholder pending counsel | Decided (implementation) |
| [0028](0028-upload-queue-design.md) | 2.4 | Local audio deleted on *pipeline* confirmation, not on `upload/complete`; offline is not a failure; storage warned in minutes not percentages | Decided (implementation) — **two status bugs found by a test's own output** |
| [0029](0029-searchable-encrypted-patient-names.md) | 2.5 | Searchable encrypted names: **the checklist's preferred blind index does not fit** (HMAC gives equality, P0-6 needs fuzzy). Decrypt-and-rank, optimised against measurements; token blind index named as the next step | Decided (implementation) — **measured, and the naive version was 2.1s** |
| [0030](0030-grounding-is-withheld-not-approximated.md) | 3 | Grounding is **withheld rather than approximated**: stored offsets re-validated against the current text, audio verified against storage rather than read from the DB, only cited passages returned, playback bounded to the cited window | Decided (implementation) — **for a proof feature, a confident wrong answer is worse than none** |
