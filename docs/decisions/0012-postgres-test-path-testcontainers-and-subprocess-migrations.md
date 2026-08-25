# 0012 — testcontainers over a CI service container; migrations run as a subprocess

**Phase:** 0.5 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** `tests/test_postgres_specific.py` uses `testcontainers[postgres]`
to spin up a disposable `postgres:16-alpine` per test session, and runs the
real `alembic upgrade head` as a **subprocess** (not `alembic.command.upgrade()`
called in-process) with `DATABASE_URL` overridden to the container's URL.

**Options considered — container source:** (a) testcontainers, as chosen;
(b) a CI-only service container (e.g. a GitHub Actions `services:` block),
with local runs left unsupported or manual. **Options considered — running
migrations:** (a) subprocess `alembic upgrade head`, as chosen; (b) call
`alembic.command.upgrade(cfg, "head")` in-process.

**Why — testcontainers over a CI service container:** (b) only runs where
someone wired the CI config for it — a service container isn't something
`pytest` run locally can reach at all, so the guarantee stays untested on a
laptop until code is pushed. (a) runs identically in both places (same
`pytest` invocation, same skip-if-no-Docker behavior locally as an
unconfigured CI runner would need anyway), and needs zero CI-specific
YAML to work today — Phase 5.3 can still add a CI service container
*in addition* later if a specific pipeline wants to avoid the Docker-in-
Docker overhead of testcontainers, but nothing here blocks that.
**Why — subprocess over in-process:** `alembic/env.py` always re-derives
its DB URL from `app.core.config.get_settings()`, which is an `lru_cache`
singleton that `tests/conftest.py` permanently points at SQLite the
instant it's first imported for this test process — there is no supported
way to point an in-process Alembic run at a different URL without
monkeypatching that cache. A subprocess gets a fresh, unpoisoned settings
singleton via its own `DATABASE_URL` env var, and — as a side benefit —
is a more honest test: it's the literal command a deploy step runs
(Phase 5.1: "run migrations as an explicit deploy step"), not an
approximation of it.

**What would change my mind:** if the subprocess-per-test-module overhead
(container start + `alembic upgrade head`, ~30-60s observed) becomes a
real friction point as more Postgres-specific tests are added, consider a
session-scoped container shared across all Postgres test modules instead
of module-scoped — but don't reach for that until it's actually slow, not
preemptively.
