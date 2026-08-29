"""Marks scripts/ as an importable package.

Phase 5.3. It exists for one reason: `scripts/check_migrations.py` is the
CI gate for migration drift, and `tests/test_migration_safety.py` asserts
the *same* check against a throwaway Postgres. Two copies of a safety
check drift apart, and the copy that drifts is always the one nobody
runs -- so the logic lives here once and both callers import it.

pytest.ini already sets `pythonpath = .`, so `from scripts.check_migrations
import ...` resolves from apps/api without any packaging changes. Existing
scripts stay runnable as plain files (`python scripts/rotate_phi_key.py`);
adding this file does not change how they are invoked.
"""
