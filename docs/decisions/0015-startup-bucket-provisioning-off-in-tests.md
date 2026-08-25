# 0015 — Startup bucket provisioning: short boto3 timeouts, and off entirely in tests

**Phase:** 1.1 · **Decided by:** implementation (perf bug found empirically) · **Date:** 2026-08-25

**Decision:** `_client()`'s boto3 `Config` sets short timeouts
(`connect_timeout=5`, `read_timeout=10`, `retries={"max_attempts": 1}`),
and a new setting `S3_PROVISION_BUCKET_ON_STARTUP` (default `True`,
forced `false` in `tests/conftest.py`) gates whether the FastAPI
`lifespan` handler calls `ensure_bucket_configured()` at all.

**What happened without this:** the first version ran
`ensure_bucket_configured()` unconditionally on every app startup with
boto3's default timeouts/retries. Every test using the `client` fixture
creates a fresh `TestClient`, which re-fires the startup event — with no
real object store listening, that's the full test suite (~50 tests) each
eating a slow connection failure. Two separate full test runs hung past
their 180-second harness timeout before this was diagnosed and fixed.

**Options considered — timeouts:** (a) short explicit timeouts on the
shared client config, as chosen; (b) leave boto3's defaults (60s connect)
and only fix the test-suite symptom. **Options considered — test
gating:** (a) an explicit settings flag, as chosen; (b) detect "are we
in pytest" implicitly (e.g. checking `sys.modules` for `pytest`) and
skip automatically.

**Why:** (a)+(a) together: a synchronous request-handling API should
never hang for a full minute because a dependency is unreachable,
independent of tests — that's a production concern the short timeout
fixes regardless of test performance. (b) for test gating is exactly the
kind of implicit, hard-to-discover magic this project's own values argue
against (compare: `require_role` existing but unattached in 0.2) — an
explicit, named setting is greppable and self-documenting in a way "if
pytest is running" isn't.

**What would change my mind:** if a real deployment's S3/MinIO
connection is ever legitimately slower than 5s to establish (a distant
region, an overloaded proxy), these timeouts would need to become
configurable rather than hardcoded — not needed yet, since local
MinIO and same-region AWS S3 are both well under this budget in
practice.
