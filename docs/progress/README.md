# Progress log

One file per phase/subphase of `docs/implementation-checklist.md`, written
right after it's implemented. Where `docs/decisions/` records *why* a
choice was made, this log records *what exists now and how it fits
together* — the durable version of the implementation walkthrough, meant to
still make sense months from now without the conversation that produced it.

Each file follows the same shape: **What changed** (files touched, and
why), **How the pieces connect** (the data/call flow between the touched
components), **Tests**, and **Notable bugs caught / open follow-ups**. Git
history has the literal diffs (`git log --oneline`, `git show <hash>`) —
these files explain them, not duplicate them.

| Phase | File | Summary |
|---|---|---|
| 0.1 | [0.1-consent-gate-enforcement.md](0.1-consent-gate-enforcement.md) | Server-side consent gate — `assert_consent_valid`, wired into `confirm_upload` and `transcribe_encounter` |
| 0.2 | [0.2-rbac-enforcement.md](0.2-rbac-enforcement.md) | `require_role` actually attached to routes; new audit-log read endpoint |
| 0.3 | [0.3-auth-hardening.md](0.3-auth-hardening.md) | Refresh-token rotation/revocation, MFA enrollment, login rate limiting/lockout |
| 0.4 | [0.4-type-consistency-drift.md](0.4-type-consistency-drift.md) | `pipeline_status`/`event` enums with real DB `CHECK` constraints; `confirm_upload` body fix |
| 0.5 | [0.5-postgres-test-path.md](0.5-postgres-test-path.md) | testcontainers-backed Postgres test path; the append-only trigger and CHECK constraints, verified for real |
| 1.1 | [1.1-upload-path.md](1.1-upload-path.md) | Presigned S3 multipart upload (init/parts/complete), real MinIO-backed tests, two bugs found by running it |
| 1.2 | [1.2-transcript-persistence.md](1.2-transcript-persistence.md) | Encrypted JSON transcript storage, wired into both ends of the Celery chain |
| 1.3 | [1.3-real-asr-integration.md](1.3-real-asr-integration.md) | Groq-hosted Whisper large-v3 (not the PRD's ElevenLabs Scribe v2) — **diarization capability lost**, turn order preserved by construction |
| — | [audit-2026-08-25-checklist-refresh.md](audit-2026-08-25-checklist-refresh.md) | `/production-checklist` refresh: mypy baseline established, a real httpx crash found and fixed, ASR vendor references brought current |
| — | [audit-2026-08-25-haiku-only.md](audit-2026-08-25-haiku-only.md) | Note generation: Haiku only, Luna dropped entirely — loses P0-4's configured-fallback risk mitigation; a stale local `.env` found in the process |
| 1.4 | [1.4-real-note-generation.md](1.4-real-note-generation.md) | `HaikuNoteGenerator` — forced-tool-call structured output, mechanical two-layer suppression, segment-ID citations verified (not trusted) before persistence |
| 1.5 | [1.5-pipeline-failure-handling.md](1.5-pipeline-failure-handling.md) | Dead-lettering (two new terminal statuses) plus a separate Celery Beat stuck-sweep; `/retry` and `/failed` routes — Phase 1 now fully closed |
| 2.1 | [2.1-web-app-foundation.md](2.1-web-app-foundation.md) | Client re-platformed to a browser web app on a clinic laptop; generated typed API client, httpOnly-cookie auth with session resume, CORS — three bugs caught by running it |
| 2.2 | [2.2-recording.md](2.2-recording.md) | Real recording: fail-closed consent gate (P0-1), AES-GCM chunks encrypted before disk, AudioWorklet gap detection, persistent indicator — 22/22 end-to-end in a real browser |
| 2.3 | [2.3-consent-flow.md](2.3-consent-flow.md) | Bilingual consent, roster, decline, mid-visit re-consent pause, withdrawal with real deletion — 35/35 end-to-end. **Script text still awaits Legal; that is blocking** |
