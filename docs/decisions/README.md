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
| [0003](0003-mid-visit-reconsent-detection.md) | 0.1 / 2.3 | Manual flag vs. diarization-based detection for mid-visit re-consent | **OPEN — your call** |
| [0004](0004-note-read-access-scope.md) | 0.2 | Note reads: authoring clinician only, or any clinician in the clinic | Decided (implementation) |
| [0005](0005-rbac-role-policy-and-audit-log-endpoint.md) | 0.2 | Per-route role policy (`doctor` vs `admin` for clinical writes); adding a minimal audit-log endpoint early | Decided (implementation) |
| [0006](0006-mobile-token-storage-and-revocation.md) | 0.3 | Where the token lives on the device; lost-phone revocation | Decided (user) |
| [0007](0007-refresh-token-lifetimes-and-reuse-detection.md) | 0.3 | Access/refresh token lifetimes; what counts as reuse vs. plain revocation | Decided (implementation) |
| [0008](0008-login-rate-limiting-backed-by-db-not-redis.md) | 0.3 | Login rate limit/lockout backed by a DB table instead of Redis | Decided (implementation) |
| [0009](0009-mfa-enrollment-bootstrap-scope.md) | 0.3 | MFA self-service enrollment scoped to first-time setup only | Decided (implementation) — **gap flagged for pilot** |
| [0010](0010-enum-check-constraints-needed-explicit-flags.md) | 0.4 | `Note.status` had no real DB constraint; enum CHECK constraints need explicit `values_callable` too | Decided (implementation) — **bug found empirically** |
| [0011](0011-encounter-pipeline-status-enum-membership.md) | 0.4 | `EncounterPipelineStatus` scoped to today's 5 values, not Phase 1.5's future ones | Decided (implementation) |
