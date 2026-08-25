# 0008 — Login rate limiting/lockout reads a DB table, not Redis

**Phase:** 0.3 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** `app/services/auth_rate_limit.py` computes both the per-IP
rate limit and the per-email lockout by counting rows in a plain
`login_attempts` table within a time window, rather than using Redis
counters (`INCR`/`EXPIRE`) even though Redis is already a dependency
(Celery's broker).

**Options considered:** (a) a Postgres/SQLite-backed `LoginAttempt` table,
as chosen; (b) Redis counters keyed by IP/email with a TTL; (c) an
in-process dict (no persistence across restarts, no multi-worker
correctness).

**Why:** (b) would make `POST /auth/login` — the one endpoint that must
work even when everything else is degraded — depend on Redis being
reachable, and it would silently pass the exact test-vs-production
divergence Phase 0.5 is written to catch: the test suite doesn't run
against a live Redis, so a Redis-backed limiter would be exercised
correctly in prod and not at all in CI. (c) is actively wrong the moment
there's more than one API worker process (which any real deployment has).
(a) is slower per-check than Redis at high volume, but a small clinic's
login volume is nowhere near where that matters, and it gets tested
identically in SQLite and Postgres for free — consistent with how the
consent ledger and audit log already work in this codebase.

**What would change my mind:** if login volume or worker count ever makes
per-login COUNT queries a measurable cost (unlikely at pilot scale, worth
checking if this becomes a multi-clinic product), move to Redis then and
add a Redis-backed test fixture at the same time — don't let the fast path
outrun what CI actually exercises.
