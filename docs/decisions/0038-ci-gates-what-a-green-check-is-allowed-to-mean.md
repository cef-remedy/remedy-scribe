# 0038 — CI gates: what a green check is allowed to mean

**Phase:** 5.3 · **Decided by:** implementation · **Date:** 2026-08-29

## The problem the checklist does not name

5.3 reads as four chores: run the linters in CI, add a migration check,
build a staging environment, ship a mobile pipeline. Three of them turn out
to be the same question asked in three places, and the fourth describes a
product that no longer exists.

The question is: **what is a passing build actually evidence of?**

Phase 4.3 built this repo's first CI and was careful about this. Its
`api` job runs `ruff`, `mypy` and 383 tests, and each of those is a real
claim about real code. But the tests build their schema with
`Base.metadata.create_all()` against SQLite, so the suite is *structurally
incapable* of noticing that a model has no migration. Add a column,
everything goes green, `alembic upgrade head` succeeds at deploy because
there is nothing to apply, and the first query against the new column
raises `UndefinedColumn` at a clinic.

Green, green, green, outage. That is not a gap in coverage — it is a
passing check asserting something it never looked at, which is worse than
having no check, because a green badge is what stops anyone looking.

So the rule this phase worked to:

> **A gate must be able to fail, must have been seen failing, and must not
> be able to pass by not running.**

Everything below is that sentence applied three times.

## 1. Migration drift: a gate that was watched going red

`scripts/check_migrations.py` brings a database to `head` and asks
Alembic's autogenerate what it would write next. Empty answer, no drift.

Three choices inside it are worth recording.

**It runs on Postgres, and refuses a SQLite URL outright.** Not for
thoroughness — because a SQLite version of this check would be *green in
exactly the cases that matter*. SQLite compiles `DateTime(timezone=True)`
and a naive `DateTime` to the same `DATETIME`, so a model that lost
`timezone=True` — against a schema whose retention clock, audit-log
expiry, and signing timestamps are all tz-aware — would produce no diff
there and a real `ALTER TABLE` here. A check that passes on the wrong
engine converts a deploy failure into one CI said could not happen.

(The first draft of that argument used `String(64)` vs `String(128)` as
the example. That is false: SQLite renders and reflects declared lengths
faithfully, it just never enforces them. A test written to pin the claim
is what caught it, which is the phase's own thesis arriving early. The
wrong version is kept as a comment in
`tests/test_migration_safety.py::test_sqlite_would_not_have_caught_this`,
because it passed review by sounding right.)

**It ran red on the first try, and the four findings are real.** Two
migrations create a `UniqueConstraint` *and* a unique index on the same
column (`transcripts.encounter_id`, `refresh_tokens.token_hash`), so a
deployed database carries a duplicate B-tree the models never asked for.
Two enum columns were widened to `Enum(native_enum=False)` in Phase 0.4
without an `ALTER`, so the deployed `VARCHAR` is wider than the model's.
None is dangerous. All four are genuine differences between a schema built
by migrations and one built by `create_all()` — and all four survived four
phases precisely *because* they are harmless, which is the argument for a
mechanical gate rather than code review.

Fixing them needs an Alembic revision, which 5.3 does not own. So:

**The baseline is a snapshot assertion, not an ignore list.** The check
requires the diff to equal `KNOWN_DIVERGENCES` **exactly** — and therefore
fails when a recorded divergence *disappears* as well as when a new one
appears. That inversion is the entire point. An ignore list only ever
grows; an entry added today is still being skipped in three years, and the
first genuine drift that happens to land on it vanishes. A snapshot has to
be edited down when someone fixes something, so the file shrinks toward
empty on its own and every surviving entry is one a human looked at
recently. Each key is the *fully rendered* diff line, so adding an
`EncounterPipelineStatus` member changes the key and fails the gate — which
is exactly when someone should check the deployed column still fits.

**And it was observed failing.** A column added to `Patient`, no revision:
the gate printed
`add_column: patients.TEMPORARY_DRIFT_PROBE (VARCHAR(24))` and exited 1,
while `pytest` on the same tree stayed green. Then reverted, byte-identical.
Decision 0034 named the principle — an untested control is a hope — and a
control nobody has watched fail is the same hope with a nicer name.

## 2. Staging: the seed exists to remove the motive for copying production

"Staging with realistic synthetic data (never production PHI)" reads as
tooling. It is a PHI control, and the reason is behavioural: the moment
someone needs to reproduce a bug and staging is three lorem-ipsum rows,
`pg_dump production | psql staging` is the obvious next command. Under the
Data Privacy Act that is an unlogged bulk disclosure of every patient in
the clinic.

You cannot police that with a policy. You remove the motive by making
staging **fuller than production feels**. Same shape of argument as
decision 0031's published dev secrets: an enforceable control beats a
stated intention, and the cheapest enforcement is removing the reason.

So "realistic" was treated as a specification, not an adjective:

- **Names are adversarial, not filler.** The matcher is `difflib` over
  decrypted names (0029) and every one of its interesting failure modes is
  name-shaped. The directory carries a compound surname, a `Ma.`
  abbreviation, a Jr./father pair distinguished *only* by birthdate, an
  unaccented `Pena` a doctor will dictate as `Peña`, two people with an
  identical name and different birthdates, Chinese-Filipino short
  surnames, and a legal name for someone everyone calls "Jun". Each row
  records which behaviour it exists to exercise. "Patient One / Patient
  Two" makes every one of those cases pass.
- **Notes are built by the production span builder.**
  `note_generation/shared.py:build_sections`, not a local copy of its
  convention. `apps/web/smoke/seed_pipeline.py` duplicates that convention
  deliberately, and for a smoke test that is the right trade — the
  duplicate is a canary. For a dataset people will trust for weeks it is
  the wrong one: change the join separator and a duplicated version
  silently produces notes whose grounding never lines up, and staging
  becomes *worse* than empty, because it looks fine.
- **Statuses are driven through `note_lifecycle.transition`**, never by
  assigning `Note.status`. Seeding around the state machine would produce
  a dataset the application itself could not have created.
- **It is verified, not asserted.** Every seeded note is read back through
  `resolve_grounding`, and a note with a live transcript that resolves
  zero cited segments fails the run.

**Pointing at production is made structurally hard, not discouraged.** Six
locks, each independently sufficient: `is_production`; an `ENVIRONMENT`
allow-list that fails *closed* (`prod-eu` is refused, not shrugged at); an
opt-in env var absent from every `.env` in the repo; a schema-at-head
check; a typed confirmation; and the one that does not depend on any
configuration being correct — **every row in `clinicians` must be on
`@staging.remedy.invalid`**. Production has real accounts, so the script
refuses before writing. `.invalid` is reserved by RFC 2606 and can never
resolve, so nothing real can match it by accident. Config can be wrong;
the contents of a table cannot lie about whose database it is.

It also **only ever INSERTs**, and there is no `--reset`. Not tidiness:
the consent ledger's append-only trigger (P0-1) means a mis-seed cannot be
cleaned up row by row *by anyone*, the table owner included. The only real
reset is dropping the database, and handing this script the privileges to
do that would give the largest possible capability to the exact code path
whose guards just failed.

### The bug the seed manufactured, and why it is in this record

Rehearsing the CI job caught the seed doing the thing Phase 3 already fixed
once. Under `--no-audio` it wrote an `audio_object_key` for bytes it never
uploaded. `grounding._audio_state` treats "row claims a key, storage says
404" as proof the lifecycle rule expired the object and stamps
`audio_deleted_at` — correct in production, where that inference is sound.
Handed a key for an upload that never happened, it recorded a **retention
expiry that never occurred**: decision 0030's confidently-wrong answer,
manufactured by the fixture meant to demonstrate it.

Fixed by making those encounters honestly never-recorded. Worth writing
down because it is the second time this system has produced a row
asserting bytes that were never there, and both times the row looked
completely ordinary.

## 3. The mobile release pipeline is obsolete, and saying so is the deliverable

> ~~Mobile release pipeline (EAS Build, internal distribution for pilot
> doctors).~~ — and "build the mobile bundle" in the first bullet.

**Decision 0024 re-platformed the client from an Expo mobile app to a
browser app on a clinic laptop, and there is no app store in the plan.**
There is no EAS project, no `eas.json`, no native bundle, no internal
distribution channel, and no TestFlight/Play track for pilot doctors.
Implementing this bullet would mean inventing a delivery mechanism for a
product that does not exist.

Three options were available and only one is honest:

1. **Build an Expo pipeline anyway.** Produces a green CI job that ships
   nothing, for a client that was retired two phases ago. This is the
   failure mode the whole record is about: a check whose passing means
   nothing.
2. **Delete the bullet silently.** Indistinguishable, six weeks later,
   from having forgotten it. Someone reviewing 5.3 against the checklist
   finds an unticked box and no explanation.
3. **Chosen: replace it with what the web client actually needs, and say
   in writing that the original is obsolete and why.**

What replaces it is deliberately small, because the honest answer is
small: `npm run build` produces a static `dist/`, and CI uploads it as a
workflow artifact (`web-client-dist-<sha>`, 14-day retention). Uploading
rather than rebuilding at deploy time is the one part that is a decision
rather than plumbing — Vite inlines `VITE_` variables at build time, so a
bundle rebuilt on the deploy host is a *different artifact* from the one
CI tested, and "we shipped what we tested" only holds if the bytes are
carried forward. Phase 5.1 owns what consumes it.

The checklist itself is outside this phase's lease, so it is not edited;
this record and `docs/progress/5.3-ci-cd.md` are where the obsolescence is
stated.

## 4. Playwright stays out of CI, as a named manual gate

`apps/web/smoke/*.cjs` need a full live stack: Postgres, Redis, MinIO, the
API, a Celery worker, Vite, a browser, and either real vendor keys or the
`SEED_PIPELINE` substitute. Both answers are defensible; the undefended
one is not.

**They stay out, and are documented as a pre-release manual gate**
(`docs/runbooks/ci-cd.md`), for one reason that is not "it is hard": a
six-process browser suite is the flakiest thing in any pipeline, and a
gate that goes red for reasons unrelated to the change is one people learn
to re-run rather than read. That habit does not stay contained — it is
learned on the flaky job and applied to the migration gate.

The cost is stated rather than hidden: **no full end-to-end path is
verified automatically today.** What CI does cover is each seam
separately — real Postgres migrations and the append-only triggers
(testcontainers), real MinIO presigned multipart (testcontainers), the
route layer against SQLite, and the client's own unit tests. The seam
nothing covers is the browser talking to a running API.

This is worth revisiting when there is a staging deploy to point at
(Phase 5.1), because the expensive half of running these in CI is standing
the stack up — not the tests.

## What would change my mind

- **On the baseline:** if `KNOWN_DIVERGENCES` is still four entries in
  three months, the snapshot has failed at the one thing that
  distinguishes it from an ignore list. It should be shrinking. An Alembic
  revision dropping the two duplicate constraints and narrowing the two
  enum columns removes all four and the mechanism goes back to asserting
  an empty diff.
- **On Postgres-only drift checking:** if the `migrations` job's service
  container proves slower or flakier on a runner than testcontainers in
  the `api` job, collapse the two — the gate matters, the second execution
  path does not.
- **On Playwright:** once 5.1's staging deploy exists, "spin up the stack"
  stops being CI's problem, and a nightly (not per-PR) run against staging
  gets the end-to-end coverage without teaching anyone to ignore a red
  check on their own pull request. That is the version I would build.
- **On the seed's guards:** lock 4 assumes `clinicians` is the table that
  proves whose database this is. If a future deploy ever ships with a
  pre-created service account on a non-`.invalid` domain in *staging*, the
  lock inverts from "refuses production" to "refuses staging" and needs a
  positive marker — a sentinel row — instead of an inference from absence.
- **On the mobile bullet:** if Remedy ever ships a native client, none of
  this argument survives, and the right move is a new decision record
  rather than an amendment to this one.
