"""Phase 5.1 (decision 0036): the probes an orchestrator acts on.

The two endpoints have *opposite* consequences — liveness failing restarts
the container, readiness failing removes it from traffic — so the tests
that matter here are the ones that pin down which endpoint reacts to what.
Getting it backwards is not a cosmetic bug: a liveness probe that checks
Postgres restarts every healthy replica at once the moment the database
blinks, and a readiness probe that checks nothing never drains anything.
"""

import pytest
from sqlalchemy.exc import OperationalError

from app.api.deps import get_db
from app.api.routes import health
from app.main import app

# A driver error carrying everything a probe response must never contain:
# a host, a private IP, a port, a database user, and — the Phase 4 lesson —
# a patient name that happened to be in flight when the connection died
# (`_extract_tool_input` interpolated a whole model response into an
# exception message that then landed in a plain column).
_LEAKY_DRIVER_MESSAGE = (
    'connection to server at "remedy-prod-db.internal" (10.0.7.21), port 5432 failed: '
    'password authentication failed for user "remedy_app"; '
    "in-flight row: patient Maria Santos Dela Cruz"
)

# Every one of these appearing anywhere in a probe body is a finding.
_MUST_NOT_APPEAR = (
    "remedy-prod-db.internal",
    "10.0.7.21",
    "5432",
    "remedy_app",
    "Maria",
    "Dela Cruz",
    "password",
    "OperationalError",
    "Traceback",
    "SELECT 1",
    "postgresql",
    "sqlite",
    "redis://",
)


class _UnreachableDatabaseSession:
    """Stands in for a Session whose server has gone away.

    Not a monkeypatch of `Session.execute`: overriding the `get_db`
    dependency is how the *application* would experience it, and it also
    proves the probe goes through the dependency at all rather than
    reaching for a connection of its own.
    """

    def execute(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception(_LEAKY_DRIVER_MESSAGE))

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture()
def database_down(client):
    """Point the app at an unreachable database for one test.

    Re-assigns the override the `client` fixture installed; the overrides
    dict is consulted per request, so this takes effect immediately and is
    cleared by `client`'s own teardown.
    """

    def _broken():
        yield _UnreachableDatabaseSession()

    app.dependency_overrides[get_db] = _broken
    return client


@pytest.fixture()
def redis_up(monkeypatch):
    """Redis reachable, without requiring a Redis.

    The default REDIS_URL points at the dev container, which exists on a
    developer machine and does not exist in CI (`.github/workflows/ci.yml`
    runs with no services). A test that needed a real broker would be a
    test that only passes on one machine.
    """
    monkeypatch.setattr(health, "check_redis", lambda: None)


@pytest.fixture()
def redis_down(monkeypatch):
    def _refuse() -> None:
        raise ConnectionError(f"Error 111 connecting to redis-prod.internal:6379. {_LEAKY_DRIVER_MESSAGE}")

    monkeypatch.setattr(health, "check_redis", _refuse)


# --- liveness ---------------------------------------------------------


def test_health(client):
    """The original Phase 0 assertion, unchanged on purpose: the probe path
    and payload survived being moved out of `main.py` into a router.
    """
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_liveness_is_up_when_the_database_is_down(database_down):
    """The single most important test in this file.

    Liveness failing means "restart the container". Postgres being
    unreachable is not a reason to restart the application — the
    application is fine, and restarting it neither fixes the database nor
    survives it. If this test ever goes red, a brief dependency outage has
    been wired to a crash loop across every replica simultaneously.
    """
    response = database_down.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_liveness_sets_no_store(client):
    """Neither probe path is under /api/v1, so SecurityHeadersMiddleware's
    `no-store` does not reach them. An intermediary caching a 200 keeps
    sending traffic to an instance that has already said it cannot serve.
    """
    assert client.get("/health").headers["cache-control"] == "no-store"
    assert client.get("/ready").headers["cache-control"] == "no-store"


# --- readiness --------------------------------------------------------


def test_readiness_is_ready_when_dependencies_answer(client, redis_up):
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": "ok", "redis": "ok"}


def test_readiness_is_down_when_the_database_is_down(database_down, redis_up):
    """503, and it names *which* dependency — the one thing the body is
    allowed to be specific about, because a check name is not a secret and
    "which of the two" is the first question an operator asks.
    """
    response = database_down.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "error"
    assert body["checks"]["redis"] == "ok"


def test_readiness_is_down_when_redis_is_down(client, redis_down):
    """Redis holds no durable state, so this is not about data loss. It is
    about `POST /uploads/complete` storing an object key and then failing
    to enqueue the pipeline: an instance that accepts a recording it cannot
    process should not be in rotation.
    """
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "error"


def test_readiness_reports_both_failures_rather_than_the_first(database_down, redis_down):
    """Both checks run even after the first fails. A probe that
    short-circuits sends an operator to restart Postgres and discover
    Redis is also down only afterwards.
    """
    response = database_down.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "error", "redis": "error"}


# --- what the body may say -------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_no_phi_or_connection_details_in_a_healthy_body(client, redis_up, path):
    _assert_no_leak(client.get(path).text)


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_no_phi_or_connection_details_in_a_failing_body(database_down, redis_down, path):
    """The probes are unauthenticated and reachable by anything that can
    reach the port. Both driver messages planted above carry a host, a
    private IP, a port, a database user and a patient name; none of it may
    reach the wire.
    """
    _assert_no_leak(database_down.get(path).text)


def _assert_no_leak(body: str) -> None:
    lowered = body.lower()
    for fragment in _MUST_NOT_APPEAR:
        assert fragment.lower() not in lowered, f"probe body leaked {fragment!r}: {body!r}"
