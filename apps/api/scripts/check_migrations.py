"""Migration safety check (Phase 5.3). Fails when the Alembic chain and the
SQLAlchemy models have stopped agreeing.

The specific failure this exists to prevent, spelled out because it is
silent in every other gate this repo has:

  1. Someone adds a column to a model in `app/models/`.
  2. `tests/conftest.py` builds the test schema with
     `Base.metadata.create_all()`, so the column exists in the test DB the
     instant the model does. **All 330 tests pass.**
  3. `ruff` and `mypy` are equally blind -- neither has an opinion about
     Alembic.
  4. Nothing generated a revision. The deploy runs `alembic upgrade head`,
     which succeeds (there is nothing new to apply), and the first query
     touching the new column raises `UndefinedColumn` in production.

Every existing check passes at every step. That is what makes it worth a
dedicated gate rather than a code-review convention.

## Two checks, not one

`check_single_head` needs no database. Two concurrent branches each adding
a revision on the same `down_revision` produce two heads; `alembic upgrade
head` then fails outright with "Multiple heads are present", which is a
better failure than a silent one but still a failure discovered at deploy
time. With three or four phases editing this repo in parallel (this file
was written while two other phases were in flight) that is not a
hypothetical.

`diff_against_models` is the real check: bring a database to `head`, then
ask Alembic's autogenerate what it *would* write next. An empty answer
means the chain fully expresses the models. A non-empty answer is a
missing revision, printed as the operations Alembic wants to perform.

## Why this must run against Postgres, not SQLite

SQLite would catch a missing table or column and miss the distinctions
Postgres actually enforces. The one this schema leans on everywhere is
timezone awareness: SQLite compiles `DateTime(timezone=True)` and a naive
`DateTime` to the same `DATETIME`, so a model losing `timezone=True` --
against a retention clock and an append-only audit log that are all
tz-aware -- is invisible there and an `ALTER TABLE` here.
(`String` lengths, for the record, are NOT an example: SQLite renders and
reflects them faithfully, it simply never enforces them. The first draft
of this docstring claimed otherwise and a test caught it.)

Since Postgres is the deploy target (decision 0012), a check that passes
on SQLite and fails on Postgres is worse than no check: it converts a
deploy failure into a deploy failure that a green CI badge said would not
happen.

So this script takes a Postgres URL and nothing else. Callers:

  * CI: `.github/workflows/ci.yml`'s `migrations` job, against a
    `services: postgres` container. This is the un-skippable path.
  * Developers and the pytest suite: `tests/test_migration_safety.py`
    spins up a throwaway Postgres via testcontainers (the same mechanism
    `tests/test_postgres_specific.py` established in Phase 0.5).

## The four divergences that already existed, and why there is a baseline

The first run of this check against the real tree was **red**, with four
findings -- all genuine, all pre-existing, none of them mine. They are
listed in `KNOWN_DIVERGENCES` below with their reasoning, and fixing them
needs an Alembic revision, which Phase 5.3 does not own.

That makes the shape of the gate a real decision, so it is stated here
rather than buried:

**This is a snapshot assertion, not an ignore list.** The check requires
the diff to equal `KNOWN_DIVERGENCES` **exactly**. New drift fails,
obviously -- but so does the *disappearance* of a recorded divergence,
which is what makes the difference. An ignore list only ever grows: an
entry added in 2026 is still being skipped in 2029, and the first real
drift that happens to match one lands in the hole. A snapshot has to be
edited down when someone fixes something, so the file shrinks toward
empty on its own and every entry in it is one somebody looked at.

Each entry is keyed by its **fully rendered** diff line, not by a coarse
`(op, table, column)` tuple. That is deliberate: the rendering of a
`modify_type` on an enum column contains every member value, so adding a
new `EncounterPipelineStatus` member changes the key and fails the gate --
which is exactly the moment someone should check that the column is still
wide enough for the longest value.

## What autogenerate does not compare, stated rather than implied

Autogenerate compares tables, columns, types, nullability, indexes and
unique constraints. It does **not** compare CHECK constraints, triggers,
grants, or (by default) server defaults. So this check cannot notice a
deleted append-only trigger or a dropped enum CHECK -- those are covered
by `tests/test_postgres_specific.py`, which asserts them behaviourally by
trying to violate them. The two suites are complementary and neither
subsumes the other.

It is also blind to a model module that `app/models/__init__.py` does not
import; see `_target_metadata`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

#: apps/api -- alembic.ini, alembic/, and the app package all live here, and
#: `alembic upgrade head` only works from this directory (alembic.ini's
#: `script_location` is relative and `prepend_sys_path = .` is what puts
#: `app` on the path for env.py).
API_ROOT = Path(__file__).resolve().parents[1]


def _target_metadata() -> Any:
    """`Base.metadata` with every model registered.

    Imported inside a function, not at module scope: importing `app.models`
    pulls in `app.core.security`, which reaches for a PHI key, and this
    module's `--help` and `check_single_head` have no business requiring
    one.

    `import app.models` rather than alembic/env.py's `from app.models
    import *` -- the star form is a syntax error inside a function, and
    `app/models/__init__.py` imports every model module for exactly this
    purpose, so the plain import registers all of them on Base.metadata
    just the same. A model module that is NOT imported there is invisible
    to Alembic autogenerate and therefore to this check, which is the one
    blind spot worth knowing about: adding `app/models/foo.py` without
    listing it in `__init__.py` produces a table that neither the
    migration chain nor this gate has ever heard of.
    """
    import app.models  # noqa: F401 - registers every model on Base.metadata
    from app.db.base import Base

    return Base.metadata


def check_single_head() -> list[str]:
    """Returns the revision chain's heads. One is correct; anything else is
    a branch that `alembic upgrade head` will refuse to apply.

    Needs no database and no PHI key, so it is the cheapest half of this
    gate and runs first.
    """
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return list(ScriptDirectory.from_config(config).get_heads())


def upgrade_to_head(database_url: str) -> subprocess.CompletedProcess[str]:
    """Runs the real `alembic upgrade head`, as a subprocess.

    A subprocess for the reason Phase 0.5 documented in
    tests/test_postgres_specific.py and it applies just as much here:
    alembic/env.py re-derives its URL from the `get_settings()` lru_cache
    singleton, which under pytest has already been permanently pointed at
    SQLite for the life of the process. A child process gets a clean
    settings singleton from its own DATABASE_URL.

    It is also the honest thing to test. `command.upgrade()` in-process is
    a different code path from the one a deploy actually executes.
    """
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def diff_against_models(database_url: str) -> list[Any]:
    """Alembic's autogenerate diff between the live schema and the models.

    Empty list == the migration chain fully expresses the models.

    `compare_metadata` excludes Alembic's own `alembic_version` table (the
    MigrationContext knows its name), so a freshly-migrated database is
    expected to produce exactly `[]` rather than a known-noise baseline.
    A tolerated-noise list would be the beginning of the end for a check
    like this -- the first real drift lands in the noise and nobody looks.
    """
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            return list(compare_metadata(context, _target_metadata()))
    finally:
        engine.dispose()


def render_diff_lines(diff: list[Any]) -> list[str]:
    """One stable, human-readable line per autogenerate operation.

    Autogenerate emits `(op_name, ...)` tuples whose payload shape varies
    by op (and, for table-level ops, a *list* of tuples), so the common
    shapes are named explicitly and anything unrecognised falls back to
    `repr` -- an unknown op must still produce a line, because a diff
    entry that renders as nothing would pass the snapshot comparison
    below while representing real drift.

    These strings are the keys of `KNOWN_DIVERGENCES`, so their format is
    load-bearing: changing it invalidates the baseline, which will fail
    loudly rather than silently, but is still not something to do casually.
    """
    lines: list[str] = []
    for entry in diff:
        entries = entry if isinstance(entry, list) else [entry]
        for item in entries:
            if not isinstance(item, tuple):
                lines.append(repr(item))
                continue
            op = item[0]
            if op in ("add_column", "remove_column"):
                _, _schema, table, column = item
                lines.append(f"{op}: {table}.{column.name} ({column.type})")
            elif op in ("add_table", "remove_table"):
                lines.append(f"{op}: {item[1].name}")
            elif op in ("add_index", "remove_index", "add_constraint", "remove_constraint"):
                lines.append(f"{op}: {getattr(item[1], 'name', item[1])}")
            elif op.startswith("modify_"):
                # (op, schema, table, column_name, kwargs, old, new)
                lines.append(f"{op}: {item[2]}.{item[3]}: {item[-2]!r} -> {item[-1]!r}")
            else:
                lines.append(f"{op}: {item[1:]!r}")
    return lines


#: Divergences that already existed when this gate was written (2026-08-28,
#: at head a0b1c2d3e4f5), each with the reason it is not being fixed here.
#: Read the "snapshot assertion, not an ignore list" section of this
#: module's docstring before adding to it -- and delete an entry the moment
#: its migration lands, because the gate fails on a stale entry too.
#:
#: Every one of these is a real difference between a schema built by
#: `alembic upgrade head` and one built by `Base.metadata.create_all()`.
#: None is functionally dangerous, which is precisely why they survived
#: four phases unnoticed and why a gate was needed to find them at all.
KNOWN_DIVERGENCES: dict[str, str] = {
    # Empty, and worth keeping empty.
    #
    # This gate found four divergences on its first contact with the real
    # tree (two redundant unique constraints shadowing unique indexes, two
    # enum columns left wider than their models by Phase 0.4). All four were
    # harmless, which is exactly why they survived four phases unnoticed --
    # nothing compared the models against the deployed schema until this
    # script existed. They were closed by migration b1c2d3e4f5a6, and this
    # baseline emptied in the same change.
    #
    # An entry here is a hole in the gate: a genuine future drift whose
    # rendered diff happens to match a recorded key would pass. So an empty
    # dict is not a formality, it is the whole value of the check. Add an
    # entry only for something you have looked at and cannot fix from the
    # phase you are in -- and say which phase, so the next person knows who
    # to hand it back to.
}


def classify_diff(diff: list[Any]) -> tuple[list[str], list[str]]:
    """Splits a rendered diff against the baseline.

    Returns (unexpected, resolved): lines the baseline does not know about,
    and baseline entries that no longer appear. Both are failures -- see
    this module's docstring on why the second one has to be.
    """
    rendered = render_diff_lines(diff)
    unexpected = [line for line in rendered if line not in KNOWN_DIVERGENCES]
    resolved = [line for line in KNOWN_DIVERGENCES if line not in rendered]
    return unexpected, resolved


_FIX_HINT = """
The models and the migration chain disagree. Generate the missing revision:

    cd apps/api
    alembic revision --autogenerate -m "<what changed>"
    # read the generated file before committing it -- autogenerate guesses
    # badly at renames (it emits drop+add, which loses data) and it does
    # not see CHECK constraints, triggers, or grants at all.
    alembic upgrade head

Do NOT add the line to KNOWN_DIVERGENCES to make this pass. That list is a
record of four pre-existing divergences with an owner, not a place to put
today's change; see scripts/check_migrations.py's docstring.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help=(
            "SQLAlchemy URL of a THROWAWAY Postgres. Defaults to $DATABASE_URL. "
            "This script runs `alembic upgrade head` against it, so never point it at a database you care about."
        ),
    )
    parser.add_argument(
        "--skip-upgrade",
        action="store_true",
        help="Assume the database is already at head (CI runs the upgrade as its own visible step).",
    )
    args = parser.parse_args(argv)

    heads = check_single_head()
    if len(heads) != 1:
        print(
            f"FAIL: the revision chain has {len(heads)} heads, not 1: {heads}\n"
            "Two branches added a revision on the same down_revision. "
            "Merge them with `alembic merge -m \"merge\" " + " ".join(heads) + "`.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: single migration head, {heads[0]}")

    if not args.database_url:
        print(
            "FAIL: no --database-url and no $DATABASE_URL. The model-vs-migration "
            "diff needs a real Postgres; see this script's docstring for why SQLite will not do.",
            file=sys.stderr,
        )
        return 2

    if not args.database_url.startswith(("postgresql", "postgres")):
        print(
            f"FAIL: {args.database_url.split('@')[-1]} is not a Postgres URL. "
            "SQLite's type affinity hides exactly the divergences this check exists to find.",
            file=sys.stderr,
        )
        return 2

    if not args.skip_upgrade:
        result = upgrade_to_head(args.database_url)
        if result.returncode != 0:
            print(
                f"FAIL: `alembic upgrade head` exited {result.returncode}\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
                file=sys.stderr,
            )
            return 1
        print("OK: alembic upgrade head")

    unexpected, resolved = classify_diff(diff_against_models(args.database_url))

    if unexpected:
        print(f"FAIL: {len(unexpected)} unrecorded difference(s) between the models and the schema at head:")
        for line in unexpected:
            print(f"  - {line}")
        print(_FIX_HINT, file=sys.stderr)
        return 1

    if resolved:
        # Not a warning. A recorded divergence that is gone means someone
        # fixed it, and leaving the entry behind would keep a real future
        # regression invisible in exactly that spot -- which is the failure
        # mode every ignore list eventually has.
        print(f"FAIL: {len(resolved)} recorded divergence(s) no longer appear in the diff:")
        for line in resolved:
            print(f"  - {line}")
        print(
            "\nSomeone fixed these. Delete their entries from KNOWN_DIVERGENCES in "
            "scripts/check_migrations.py so the gate keeps watching that spot.",
            file=sys.stderr,
        )
        return 1

    if KNOWN_DIVERGENCES:
        print(
            f"OK: no new drift ({len(KNOWN_DIVERGENCES)} recorded pre-existing "
            "divergence(s) still present -- see KNOWN_DIVERGENCES)"
        )
    else:
        print("OK: no drift -- the migration chain fully expresses the models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
