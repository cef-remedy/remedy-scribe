# Runbook — CI/CD

**Phase:** 5.3 · **Status:** written and **rehearsed locally, step by
step**. As of 2026-08-29 this workflow has **never executed on a GitHub
runner** — no remote exists yet. The first push is the first real run.
**Related:** [0038](../decisions/0038-ci-gates-what-a-green-check-is-allowed-to-mean.md),
[0034](../decisions/0034-an-untested-control-is-a-hope.md),
[0024](../decisions/0024-web-client-on-laptop-not-mobile.md) (why there is
no mobile pipeline), [staging](staging.md),
[secrets-management](secrets-management.md)

> Read the status line above before trusting anything below. Every command
> in this workflow has been run by hand on a developer machine and the
> YAML has been parsed and structurally asserted, but **"rehearsed
> locally" and "known to work on a runner" are different claims** and this
> document will not blur them. §7 lists exactly what remains unverified.

## 1. What CI is, in one table

`.github/workflows/ci.yml`, five jobs, all `ubuntu-latest`, all
independent (no `needs:`), running on push to `main`, on every pull
request, on a weekly schedule, and on `workflow_dispatch`.

| Job | What it proves | Can it skip? |
|---|---|---|
| `api` | `ruff check .`, `mypy app`, the full pytest suite — including the testcontainers Postgres and MinIO suites | Partly — see §4 |
| `migrations` | `alembic upgrade head`, no model/migration drift, and the staging seed still satisfies the real constraints | **No** |
| `api-audit` | `pip-audit` against the pinned requirements | No |
| `web` | `npm ci`, typecheck, build, vitest, **and uploads the built client bundle** | No |
| `web-audit` | `npm audit --audit-level=high` | No |

The weekly schedule exists for the two audit jobs. A new advisory lands
against code that did not change, so an audit that only fires on push is
silent exactly on the weeks that matter.

**No job needs a secret.** `tests/conftest.py` generates its own PHI key;
the `migrations` job uses the key published on purpose in
`apps/api/.env.example` (decision 0031), which the app refuses to start
with in production. A CI job holding a real PHI key would be the largest
single-secret exposure in the system.

## 2. The migration gate — the one thing to understand

This is the gate 5.3 was asked for, and the reason it exists is that
**everything else in the workflow is structurally blind to it.**

`tests/conftest.py` builds the test schema with
`Base.metadata.create_all()`. Add a column to a model and it exists in the
test database the instant the model does. The whole suite passes. `ruff`
and `mypy` have no opinion about Alembic. The deploy runs
`alembic upgrade head`, which succeeds because there is nothing new to
apply, and the first query touching the column raises `UndefinedColumn`
against a live clinic database.

The `migrations` job runs three steps against a real Postgres service
container:

```bash
alembic upgrade head                              # the real deploy step
python scripts/check_migrations.py --skip-upgrade # the drift gate
python scripts/seed_staging.py --yes --no-audio   # the seed still works
```

**When it fails, this is what to do.** The output names the operations
Alembic wants to perform:

```
FAIL: 1 unrecorded difference(s) between the models and the schema at head:
  - add_column: patients.middle_name (VARCHAR(64))
```

```bash
cd apps/api
alembic revision --autogenerate -m "patients.middle_name"
# READ the generated file. Autogenerate renders a rename as drop+add,
# which loses data, and it cannot see CHECK constraints, triggers, or
# grants at all.
alembic upgrade head
```

**Do not add the line to `KNOWN_DIVERGENCES` to make it pass.** That list
is a dated record of four pre-existing divergences with an owner
(§3), not a place to put today's change.

Two other failure modes it reports:

- **`the revision chain has 2 heads`** — two branches added a revision on
  the same `down_revision`. `alembic upgrade head` refuses to run at all
  in that state. Merge them: `alembic merge -m "merge" <rev1> <rev2>`.
- **`N recorded divergence(s) no longer appear in the diff`** — someone
  *fixed* one of the four known divergences. Delete its entry from
  `KNOWN_DIVERGENCES`. This is a deliberate failure, not a bug; see §3.

To run the gate locally you need a throwaway Postgres:

```bash
docker compose -f infra/docker-compose.yml up -d postgres
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U remedy -d postgres -c "CREATE DATABASE remedy_migcheck OWNER remedy;"
cd apps/api
DATABASE_URL="postgresql+psycopg://remedy:remedy@localhost:5433/remedy_migcheck" \
  python scripts/check_migrations.py
```

Or just `pytest tests/test_migration_safety.py`, which spins up its own
container via testcontainers.

## 3. The four known divergences, and the rule about them

The gate ran red the first time it was pointed at the real tree. All four
findings are genuine differences between a schema built by
`alembic upgrade head` and one built by `create_all()`:

| Divergence | Cause | Real cost |
|---|---|---|
| `remove_constraint: transcripts_encounter_id_key` | `f6a7b8c9d0e1` creates a `UniqueConstraint` **and** a unique index on `encounter_id`; the model declares only the index | A second B-tree maintained on every insert |
| `remove_constraint: refresh_tokens_token_hash_key` | Same pattern in `c3d4e5f6a7b8` | Same |
| `modify_type: consent_ledger_entries.event` | `String(16)` in the initial schema, `Enum(native_enum=False)` since 0.4, no `ALTER` | Deployed column is 7 chars wider than the model asks |
| `modify_type: encounters.pipeline_status` | Same | Deployed column is `VARCHAR(32)`, longest member is 20 chars |

None is dangerous, and that is why they survived four phases: nothing
mechanical was looking. Fixing them is a single Alembic revision
(`op.drop_constraint` ×2, `op.alter_column` ×2), owned by whoever next
touches `alembic/`.

**`KNOWN_DIVERGENCES` is a snapshot, not an ignore list.** The gate fails
when an entry *disappears* as well as when a new line appears, so the list
has to be edited down when something is fixed and shrinks toward empty on
its own. Each key is the fully rendered diff line, so adding an
`EncounterPipelineStatus` member changes the key and fails the gate on
purpose — that is the moment to check the deployed column still fits.

## 4. What can still skip, and what was done about it

`pytest.ini` marks the testcontainers suites `postgres` and `s3`, and both
**skip rather than fail** when Docker is unreachable. On a GitHub ubuntu
runner Docker is present, so they should run — but "should" is doing work
in that sentence, and a safety gate that quietly skips when its dependency
is missing is indistinguishable from one that passed.

Two things reduce that:

- The `api` job sets `REMEDY_REQUIRE_POSTGRES=1`, which turns
  `tests/test_migration_safety.py`'s skip into a hard failure. **It covers
  that file only.** `test_postgres_specific.py` and
  `test_storage_specific.py` still skip silently; extending the same
  switch to them needs `tests/conftest.py`, which 5.3 does not own.
- The `migrations` job does not use testcontainers at all. It uses a
  GitHub `services:` container, which either comes up or fails the job.
  That is the un-skippable path for the migration gate specifically.

**Check this on the first real run.** If the `api` job's summary shows
skips, the testcontainers path is not working on the runner and the
raw-SQL migration assertions (the consent ledger's append-only trigger,
0.4's CHECK constraints) are not being exercised — which is the entire
reason those suites exist.

## 5. The client bundle — and the mobile pipeline that does not exist

The `web` job uploads `apps/web/dist` as `web-client-dist-<sha>`,
14 days' retention, `if-no-files-found: error`.

**The checklist's "build the mobile bundle" and "mobile release pipeline
(EAS Build, internal distribution for pilot doctors)" are obsolete.**
Decision 0024 re-platformed the client from an Expo app to a browser app
on a clinic laptop, and there is no app store in the plan. There is no EAS
project, no `eas.json`, no native bundle, no TestFlight or Play track. The
static `dist/` above is the whole of what replaces them. See
[decision 0038](../decisions/0038-ci-gates-what-a-green-check-is-allowed-to-mean.md)
§3 for why this is stated rather than silently dropped.

**Why the artifact is uploaded rather than rebuilt at deploy time.** Vite
inlines `VITE_` variables at build time, so a bundle rebuilt on the deploy
host is a *different artifact* from the one CI tested. "We shipped what we
tested" only holds if the bytes are carried forward.

Two things Phase 5.1 needs to know about these bytes:

- The build currently uses Vite's defaults, because **no staging API URL
  exists yet.** Whatever 5.1 settles on has to be present at build time,
  which means either CI learns the value or 5.1 rebuilds — and if it
  rebuilds, this artifact is a convenience, not the release.
- The bundle includes a **service worker** (`sw.js` + a workbox precache
  manifest, ~343 KB total across 8 precached entries). Serving a new
  `index.html` with a stale `sw.js`, or the reverse, is the classic way a
  PWA pins users to an old build. Deployment must replace the whole
  directory atomically.

## 6. Playwright smoke suites: a manual gate, on purpose

`apps/web/smoke/*.cjs` (`auth-flow`, `consent-flow`, `record-flow`,
`upload-queue`, `note-flow`, `grounding-flow`) are **not in CI**, and that
is a decision rather than an omission.

They need Postgres, Redis, MinIO, the API, a Celery worker, Vite, a
browser, and either real vendor keys or `SEED_PIPELINE`. A six-process
browser suite is the flakiest thing in any pipeline, and a gate that goes
red for reasons unrelated to the change is one people learn to re-run
rather than read — a habit that does not stay contained on the job that
taught it.

**So they are a pre-release manual gate.** Run them before any deploy that
touches recording, consent, upload, or grounding:

```bash
docker compose -f infra/docker-compose.yml up -d postgres redis minio
cd apps/api && alembic upgrade head
uvicorn app.main:app --reload &
celery -A app.tasks.celery_app worker --loglevel=info &
cd ../web && npm run dev &
node smoke/auth-flow.cjs        # then consent, record, upload-queue,
                                # note, grounding
```

**What this leaves uncovered, stated plainly:** no full end-to-end path is
verified automatically today. CI covers each seam separately — real
Postgres migrations and triggers, real MinIO presigned multipart, the
route layer on SQLite, the client's unit tests — but nothing covers a
browser talking to a running API. That gap closes cheaply once 5.1's
staging deploy exists, because the expensive half of running these in CI
is standing the stack up, not the tests. A **nightly** run against staging
(not per-PR) is the version to build.

## 7. What is unverified until a runner executes this

Listed by name rather than left implicit, because §1's status line is only
honest if this section exists:

- **Nothing here has run on GitHub infrastructure.** Steps were executed
  by hand locally; the YAML was parsed with PyYAML and structurally
  asserted (5 jobs, all `ubuntu-latest`, four triggers, expected step
  counts, the service-container block, the artifact upload's `path` and
  `if-no-files-found`).
- **`actionlint` is not available in this environment**, so action *input
  names* and the runner API were never schema-checked. A typo in a `with:`
  key would not have been caught by anything above. The likeliest first
  failures are in the pieces newest to this workflow: the `services:`
  block's `options:` health-check syntax, and `actions/upload-artifact@v4`
  input names.
- **The Postgres service container's health-check timing** is unrehearsed.
  Locally the database was already up; on a runner `alembic upgrade head`
  runs as soon as the health check passes, and if that races, the
  `migrations` job fails on connection rather than on drift. If it does,
  that is the fix — not a retry loop around the gate.
- **Whether the testcontainers suites actually run on the runner** (§4).
- **Timing.** Locally the API suite takes ~90 s and the web job ~30 s. A
  runner is slower and installs dependencies cold. No timeout is set on
  any job, so a hang would run to GitHub's 6-hour default.
- **Actions are pinned to major tags, not commit SHAs.** SHA-pinning is
  the stronger supply-chain posture and remains the intended follow-up
  (inherited from Phase 4.3); it is not done because the SHAs could not be
  verified from this environment, and a pin nobody checked is worse than
  an honest tag.

## 8. Deliberate non-goals

- **No `ruff format --check`.** The repo is not format-clean — 44 of 98
  files would be rewritten at 120 columns (Phase 4.3 measured it) — and
  making it so is a mechanical commit of its own. Adding the check now
  would either fail the build or bury a real change under a 44-file
  reformat. `ruff format` should not be run casually until that commit
  lands deliberately.
- **No coverage gate.** A percentage is a target that gets met by testing
  what is easy. The gates here assert specific properties instead.
- **No deploy step.** CI stops at "the artifact exists and the checks
  pass." Phase 5.1 owns delivery.
