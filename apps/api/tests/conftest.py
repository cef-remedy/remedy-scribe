"""Test settings: SQLite file DB + a generated PHI key, both set via
env vars *before* app.core.config is imported anywhere, since
get_settings() is an lru_cache singleton read once per process.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet

# One shared SQLite file per checkout, which is fine for one test run at a
# time and silently corrupting for two: `_fresh_schema` below drops and
# recreates every table per test, so a second concurrent run sees tables
# vanish mid-test and fails with "no such table" in unrelated files.
# TEST_DB_PATH lets a run opt into its own file. Default is unchanged.
_DB_PATH = Path(os.environ.get("TEST_DB_PATH") or Path(__file__).parent / "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.setdefault("PHI_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-secret")
# Phase 1.1: the `client` fixture below re-fires FastAPI's startup event
# on every single test (a fresh TestClient each time) — with no real S3
# endpoint running, that's ~50 tests x several failed network calls
# each, easily minutes of nothing but connection failures. Route-level
# upload tests monkeypatch app.services.storage directly instead; the
# real bucket-provisioning path is exercised for real in
# tests/test_storage_specific.py against an actual MinIO container.
os.environ["S3_PROVISION_BUCKET_ON_STARTUP"] = "false"
# Pinned, not left to fall through to whatever's in a developer's own
# apps/api/.env — test_self_service_auth.py has both a `require_mfa=True`
# baseline (no monkeypatch) and explicit `require_mfa=False` cases
# (monkeypatch.setattr per-test). The baseline tests assume True; a local
# .env with REQUIRE_MFA=false (added for demo/local testing convenience)
# silently flipped that baseline and broke them with no code change at
# all — caught live when a developer's own .env picked up exactly that
# override. Blind overwrite, like DATABASE_URL above: the whole point is
# that nothing outside this file gets a vote on the test suite's baseline.
os.environ["REQUIRE_MFA"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_db  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_schema():
    """Recreate every table before each test — cheap on SQLite, and keeps
    tests independent without needing per-test transaction rollback.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db):
    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
