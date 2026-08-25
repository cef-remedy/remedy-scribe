"""Phase 0.5: close the test-vs-production divergence.

The rest of this suite runs on SQLite (tests/conftest.py) for speed, and
that's a reasonable trade in general — but it silently voids every
Postgres-specific guarantee, because `Base.metadata.create_all()` builds
tables straight from the ORM models and never touches a migration file.
The consent ledger's append-only trigger and 0.4's CHECK constraints
both live in Alembic migrations as raw SQL; SQLite has run zero of them,
ever, in this test suite. Someone could drop the trigger migration
tomorrow and every other test would still pass.

These tests close that specific gap: spin up a real, disposable Postgres
via testcontainers, run the *actual* `alembic upgrade head` against it
(as a subprocess — not `command.upgrade()` in-process, since
alembic/env.py always re-derives its DB URL from the cached
`app.core.config.get_settings()` singleton, which tests/conftest.py has
already permanently pointed at SQLite for this process; a subprocess
gets a fresh, unpoisoned settings singleton via its own DATABASE_URL),
and assert against what that real migration chain actually produces.

Requires a running Docker daemon. Skips (doesn't fail) when Docker is
unreachable, so the rest of the suite stays runnable without it — see
`postgres_engine` below. Run just these with `pytest -m postgres`; skip
them explicitly with `pytest -m "not postgres"`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.clinician import Clinician
from app.models.consent import ConsentEventType, ConsentLedgerEntry
from app.models.encounter import Encounter
from app.models.note import Note

_API_ROOT = Path(__file__).resolve().parents[1]  # apps/api

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def postgres_engine():
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers is not installed — see requirements-dev.txt")

    try:
        container = PostgresContainer("postgres:16-alpine", driver="psycopg")
        container.start()
    except Exception as exc:  # noqa: BLE001 - any Docker-unavailable reason should skip, not fail the suite
        pytest.skip(f"Docker/Postgres container unavailable, skipping Postgres-backed tests: {exc}")

    try:
        db_url = container.get_connection_url()

        # The real deploy step, not a Python API call — this is what
        # actually proves "test against what you deploy" (0.5's own
        # framing). A failure here is a genuine migration bug, not an
        # environment problem, so it's a hard failure, not a skip.
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=_API_ROOT,
            env={**os.environ, "DATABASE_URL": db_url},
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(
                f"alembic upgrade head failed against the test container "
                f"(exit {result.returncode}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

        engine = create_engine(db_url)
        yield engine
        engine.dispose()
    finally:
        container.stop()


@pytest.fixture()
def postgres_session(postgres_engine):
    session = sessionmaker(bind=postgres_engine)()
    try:
        yield session
    finally:
        session.close()


def _seed_encounter(session) -> Encounter:
    clinician = Clinician(
        email=f"doc-{uuid.uuid4()}@example.com",
        full_name="Dr. Reyes",
        hashed_password="x",
    )
    session.add(clinician)
    session.commit()

    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key=f"idem-{uuid.uuid4()}")
    session.add(encounter)
    session.commit()
    return encounter


def _seed_consent_entry(session, encounter: Encounter) -> ConsentLedgerEntry:
    entry = ConsentLedgerEntry(
        encounter_id=encounter.id,
        event=ConsentEventType.GIVEN,
        participant_roster="[]",
        purposes="[]",
        script_language="en",
    )
    session.add(entry)
    session.commit()
    return entry


# --- the checklist's explicit ask: the append-only trigger, for real ------


def test_consent_ledger_rejects_update(postgres_session):
    encounter = _seed_encounter(postgres_session)
    entry = _seed_consent_entry(postgres_session, encounter)

    with pytest.raises(DBAPIError) as exc_info:
        postgres_session.execute(
            text("UPDATE consent_ledger_entries SET event = 'withdrawn' WHERE id = :id"), {"id": entry.id}
        )
        postgres_session.commit()
    postgres_session.rollback()

    # Not just "some error" — the specific trigger this test exists for.
    assert "append-only" in str(exc_info.value)


def test_consent_ledger_rejects_delete(postgres_session):
    encounter = _seed_encounter(postgres_session)
    entry = _seed_consent_entry(postgres_session, encounter)

    with pytest.raises(DBAPIError) as exc_info:
        postgres_session.execute(text("DELETE FROM consent_ledger_entries WHERE id = :id"), {"id": entry.id})
        postgres_session.commit()
    postgres_session.rollback()

    assert "append-only" in str(exc_info.value)


# --- bonus: 0.4's CHECK constraints, verified against the real deploy -----
# target rather than only SQLite (tests/test_schema_constraints.py already
# covers these on SQLite; this is the same claim, tested where it matters).


def test_encounter_pipeline_status_check_constraint(postgres_session):
    encounter = _seed_encounter(postgres_session)

    with pytest.raises(IntegrityError):
        postgres_session.execute(
            text("UPDATE encounters SET pipeline_status = 'bogus' WHERE id = :id"), {"id": encounter.id}
        )
        postgres_session.commit()
    postgres_session.rollback()


def test_consent_event_check_constraint(postgres_session):
    """Deliberately an INSERT, not an UPDATE. The append-only trigger
    (0.1) is BEFORE UPDATE OR DELETE — it intercepts any UPDATE to this
    table before the CHECK constraint ever gets a chance to look at the
    new value, so an UPDATE-shaped version of this test would fail with
    the trigger's "append-only" error instead of a CHECK violation. That
    was the first version of this test, and it's *why* this comment is
    here: found by actually running it against real Postgres, not by
    reasoning about the migrations in isolation.
    """
    encounter = _seed_encounter(postgres_session)

    with pytest.raises(IntegrityError):
        postgres_session.execute(
            text(
                "INSERT INTO consent_ledger_entries "
                "(id, encounter_id, event, participant_roster, purposes, script_language, created_at) "
                "VALUES (:id, :encounter_id, 'bogus', '[]', '[]', 'en', now())"
            ),
            {"id": str(uuid.uuid4()), "encounter_id": encounter.id},
        )
        postgres_session.commit()
    postgres_session.rollback()


def test_note_status_check_constraint(postgres_session):
    encounter = _seed_encounter(postgres_session)
    note = Note(encounter_id=encounter.id, note_generator_provider="haiku")
    postgres_session.add(note)
    postgres_session.commit()

    with pytest.raises(IntegrityError):
        postgres_session.execute(text("UPDATE notes SET status = 'bogus' WHERE id = :id"), {"id": note.id})
        postgres_session.commit()
    postgres_session.rollback()
