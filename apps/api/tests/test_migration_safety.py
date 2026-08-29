"""Phase 5.3: the migration chain and the models must not drift apart.

The gap this closes is the one every other gate in this repo is blind to,
and it is worth restating because the blindness is total rather than
partial:

    Add a column to a model. `tests/conftest.py` builds the test schema
    with `Base.metadata.create_all()`, so the column exists the instant
    the model does -- all 330 tests pass. `ruff` and `mypy` have no
    opinion about Alembic. The deploy runs `alembic upgrade head`, which
    succeeds because there is nothing new to apply, and the first query
    touching the column raises `UndefinedColumn` against a live clinic
    database.

Green, green, green, outage.

The check itself lives in `scripts/check_migrations.py` because CI runs it
there as a standalone gate against a `services: postgres` container, where
it cannot skip. This file is the same assertion on the developer path,
using the testcontainers mechanism Phase 0.5 established
(`tests/test_postgres_specific.py`, decision 0012) so `pytest` alone is
enough to catch the mistake before it is pushed.

**Postgres, not SQLite, and not negotiable.** SQLite compiles
`DateTime(timezone=True)` and a naive `DateTime` to the same `DATETIME`,
so a model that silently lost `timezone=True` -- against a schema whose
retention clock and append-only audit log are all tz-aware -- produces no
SQLite diff and a real `ALTER TABLE` on Postgres. A check that is green on
the wrong engine is worse than no check, because it converts a deploy
failure into one that a passing CI said could not happen. See
`test_sqlite_would_not_have_caught_this` below, which pins the claim
instead of asserting it in prose.

Set `REMEDY_REQUIRE_POSTGRES=1` to turn the Docker-unavailable skip into a
failure. CI sets it. A safety check that skips silently is the same
category of thing as a safety check nobody has watched fail.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_migrations import (
    KNOWN_DIVERGENCES,
    check_single_head,
    classify_diff,
    diff_against_models,
    upgrade_to_head,
)

_API_ROOT = Path(__file__).resolve().parents[1]  # apps/api

pytestmark = pytest.mark.postgres


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("REMEDY_REQUIRE_POSTGRES") in ("1", "true", "True"):
        pytest.fail(f"REMEDY_REQUIRE_POSTGRES is set, so this may not skip: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def migrated_postgres_url() -> str:
    """A throwaway Postgres brought to `head` by the real Alembic CLI.

    A subprocess rather than `command.upgrade()`, for the reason 0.5
    documented: alembic/env.py re-derives its URL from the `get_settings()`
    lru_cache singleton, and `tests/conftest.py` has already pointed that
    singleton at SQLite for the life of this process. A child process gets
    a clean one from its own DATABASE_URL. It is also the code path a
    deploy actually runs, which is the whole point of testing it.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        _skip_or_fail("testcontainers is not installed -- see requirements-dev.txt")

    try:
        container = PostgresContainer("postgres:16-alpine", driver="psycopg")
        container.start()
    except Exception as exc:  # noqa: BLE001 - any Docker-unavailable reason should skip, not fail
        _skip_or_fail(f"Docker/Postgres container unavailable: {exc}")

    try:
        url = container.get_connection_url()
        result = upgrade_to_head(url)
        if result.returncode != 0:
            # A hard failure, never a skip: the migrations not applying is
            # the single most deploy-relevant thing this file can discover.
            pytest.fail(
                f"alembic upgrade head failed (exit {result.returncode}):\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        yield url
    finally:
        container.stop()


def test_exactly_one_migration_head():
    """Two phases adding a revision on the same `down_revision` produce two
    heads, and `alembic upgrade head` then refuses to run at all. Cheap,
    needs no database, and this repo has had three or four phases editing
    it concurrently -- so it is not a hypothetical.
    """
    heads = check_single_head()
    assert len(heads) == 1, (
        f"the revision chain has {len(heads)} heads: {heads}. "
        f'Merge them with `alembic merge -m "merge" {" ".join(heads)}`.'
    )


def test_no_undeclared_schema_drift(migrated_postgres_url):
    """The real gate: after `alembic upgrade head`, autogenerate must want
    to write nothing that is not already recorded in `KNOWN_DIVERGENCES`.

    Observed failing before it was trusted (docs/progress/5.3-ci-cd.md): a
    column added to `Patient` produced
    `add_column: patients.TEMPORARY_DRIFT_PROBE (VARCHAR(24))` here while
    the rest of the suite stayed green.
    """
    unexpected, _ = classify_diff(diff_against_models(migrated_postgres_url))
    assert not unexpected, (
        "The models and the migration chain disagree:\n  - "
        + "\n  - ".join(unexpected)
        + '\n\nGenerate the missing revision: `alembic revision --autogenerate -m "..."`, '
        "read it (autogenerate renders a rename as drop+add, which loses data), then "
        "`alembic upgrade head`. Do not add the line to KNOWN_DIVERGENCES."
    )


def test_recorded_divergences_are_still_real(migrated_postgres_url):
    """The half that keeps `KNOWN_DIVERGENCES` from becoming an ignore list.

    An ignore list only grows, and the first genuine drift that happens to
    match a stale entry disappears into it. Failing when a recorded
    divergence is *fixed* forces the list to shrink toward empty, so every
    entry left in it is one somebody has looked at recently.
    """
    _, resolved = classify_diff(diff_against_models(migrated_postgres_url))
    assert not resolved, (
        "These divergences are recorded in scripts/check_migrations.py but no longer appear:\n  - "
        + "\n  - ".join(resolved)
        + "\n\nSomeone fixed them. Delete their entries so the gate keeps watching that spot."
    )


def test_migrations_are_the_only_way_the_schema_is_built():
    """Guards the assumption the two tests above rest on.

    `diff_against_models` compares the live schema against
    `Base.metadata`, and `Base.metadata` is only complete because
    `app/models/__init__.py` imports every model module. A new
    `app/models/foo.py` that nobody adds to that file is invisible to
    Alembic autogenerate *and* to this suite -- the one blind spot in the
    gate, so it gets an assertion of its own rather than a comment.
    """
    import app.models

    module_names = {
        path.stem
        for path in (_API_ROOT / "app" / "models").glob("*.py")
        if path.stem not in ("__init__", "mixins")
    }
    imported = {
        cls.__module__.rsplit(".", 1)[-1] for cls in (getattr(app.models, name) for name in app.models.__all__)
    }
    missing = module_names - imported
    assert not missing, (
        f"app/models/{{{','.join(sorted(missing))}}}.py define models that "
        "app/models/__init__.py does not import. Alembic autogenerate cannot see them, "
        "so they will never get a migration and this drift check will never notice."
    )


def test_check_migrations_script_runs_green_end_to_end(migrated_postgres_url):
    """Runs `scripts/check_migrations.py` the way CI invokes it.

    The functions above are imported and called directly, which does not
    exercise argument parsing, the Postgres-URL guard, or the exit codes CI
    depends on. This is the same distinction as 0.5's "run the real alembic
    CLI, not command.upgrade()": the thing that ships is the entry point,
    not the library behind it.
    """
    result = subprocess.run(
        [sys.executable, "scripts/check_migrations.py", "--database-url", migrated_postgres_url, "--skip-upgrade"],
        cwd=_API_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "no new drift" in result.stdout or "no drift" in result.stdout


def test_sqlite_would_not_have_caught_this():
    """Not a tautology -- it pins the reason this suite costs a container.

    The first version of this test claimed `String(64)` vs `String(128)`
    was invisible to SQLite. It is not: SQLite renders and reflects the
    declared length faithfully, it just never enforces it. Kept as a note
    because the wrong version passed review by sounding right.

    The divergence that is genuinely invisible, and that this schema leans
    on everywhere, is timezone awareness: every timestamp column here is
    `DateTime(timezone=True)`, and SQLite compiles that to plain `DATETIME`
    either way. A model losing `timezone=True` -- which would silently
    start storing naive timestamps against a retention clock and an
    append-only audit log -- produces no SQLite diff at all and a real
    `ALTER TABLE` on Postgres.
    """
    from sqlalchemy import DateTime
    from sqlalchemy.dialects import postgresql, sqlite

    sqlite_dialect, postgres_dialect = sqlite.dialect(), postgresql.dialect()
    aware, naive = DateTime(timezone=True), DateTime(timezone=False)

    assert aware.compile(sqlite_dialect) == naive.compile(sqlite_dialect) == "DATETIME"
    assert aware.compile(postgres_dialect) != naive.compile(postgres_dialect)


def test_known_divergences_each_carry_a_reason():
    """A baseline entry without an explanation is an ignore list entry."""
    for line, reason in KNOWN_DIVERGENCES.items():
        assert len(reason) > 80, f"KNOWN_DIVERGENCES[{line!r}] needs a real explanation, not {reason!r}"
