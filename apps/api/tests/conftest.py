"""Test settings: SQLite file DB + a generated PHI key, both set via
env vars *before* app.core.config is imported anywhere, since
get_settings() is an lru_cache singleton read once per process.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet

_DB_PATH = Path(__file__).parent / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ.setdefault("PHI_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-secret")

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
