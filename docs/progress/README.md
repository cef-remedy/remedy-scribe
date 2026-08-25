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
