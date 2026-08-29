"""Liveness and readiness probes (Phase 5.1, decision 0036).

Two endpoints, because an orchestrator asks two different questions and
acts in opposite ways on the answers:

- **`GET /health` — liveness. "Is this process wedged?"** A failure here
  means *restart the container*. It therefore checks **nothing external**,
  on purpose. A liveness probe that touches Postgres restarts a perfectly
  healthy application because the database blinked — and it restarts
  *every* replica at once, since they all share the dependency. That turns
  a 30-second dependency outage into a crash loop, and a crash loop into
  an incident that outlives its cause. The failure this probe exists to
  catch is the one a restart actually fixes: the event loop deadlocked, the
  process out of file descriptors, the thing that cannot answer at all.

- **`GET /ready` — readiness. "Can this instance serve traffic right now?"**
  A failure here means *take it out of the load balancer and leave it
  running.* Nothing is restarted, no state is lost, and the instance comes
  back on its own the moment the dependency does. This is where dependency
  checks belong, and the only place they are safe.

Both are unauthenticated and unversioned (no `/api/v1` prefix): a probe is
issued by the orchestrator before any credential exists, and moving a probe
path across an API version bump would silently un-monitor the deployment.
The cost of that choice is that these two bodies are readable by anything
that can reach the port, which is why §"what the body may say" below is a
hard rule rather than a style preference.

**What the body may say.** Check results are the literals `"ok"` and
`"error"` and nothing else. No exception text, no host, no port, no
database name, no driver message. `psycopg` and `redis-py` both put the
connection target — and `psycopg` sometimes the user — into their error
strings, so forwarding an exception message here would publish
`DATABASE_URL` to an unauthenticated endpoint. And an exception raised
while a PHI row is in flight can carry that row (Phase 4 found exactly
this: `_extract_tool_input` interpolated a whole model response, PHI
included, into an exception message that then got written to a plain
column). The diagnosis goes to the application log, which is authenticated
by virtue of being on the host; the probe gets a verdict.
"""

from __future__ import annotations

import logging

import redis
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(tags=["ops"])

# A probe has to answer inside the orchestrator's own probe timeout, or it
# fails as a timeout rather than as a "not ready" — which reads identically
# in the outcome but not in the logs, and is much harder to diagnose. The
# two checks run in series, so the worst case is their sum; Caddy's probe
# timeout (infra/caddy/Caddyfile) is set to 10 s against a measured 4.1 s
# worst case rather than to something that looks tidy.
#
# Redis' timeout is set here because redis-py takes it as a client
# argument. Postgres' equivalent cannot be: it is `connect_timeout` in the
# libpq connection string, so it belongs to DATABASE_URL and is set in
# infra/env/production.env.example.
#
# Measured, because the numbers are not intuitive (docs/progress/5.1):
# with the local Postgres container stopped, a readiness probe answers in
# **4.1 s** — the port is closed, so nothing is timing out; libpq is
# resolving `localhost` to both ::1 and 127.0.0.1 and paying ~2 s on each
# in turn. Against `127.0.0.1` the same probe answers in 2.0 s.
#
# Two things follow. **The probe budget is connect_timeout x the number of
# addresses the hostname resolves to**, not connect_timeout — a managed
# Postgres endpoint with an A and a AAAA record doubles it silently. And
# `connect_timeout` did not shorten any of this (libpq floors it at 2 s,
# and a refused connection was already faster than the floor); what it
# buys is a bound on the case that cannot be reproduced by stopping a
# container — a host that accepts the SYN and never finishes the
# handshake, which is what a security-group change or a mid-flight
# failover looks like. Unbounded there means minutes.
_PROBE_TIMEOUT_SECONDS = 2.0

_OK = "ok"
_ERROR = "error"


def check_database(db: Session) -> None:
    """Round-trip the smallest possible query through the *application's
    own* engine and pool.

    Deliberately the pooled session the routes use, not a fresh
    connection: a healthy Postgres reached through an exhausted connection
    pool is an instance that cannot serve traffic, and a probe that opens
    its own connection would report ready throughout. Checking the path
    traffic actually takes is the whole point of a readiness probe.
    """
    db.execute(text("SELECT 1")).scalar_one()


def check_redis() -> None:
    """PING the Celery broker.

    Redis holds no durable state in this system (decision 0036) — nothing
    here is about data integrity. It is about a request that *looks* like
    it succeeded: with the broker unreachable, `POST /uploads/complete`
    stores the object key, then raises when `run_pipeline.delay()` cannot
    enqueue. An instance that accepts a recording it cannot process is
    worse than one that declines the request, so this gates traffic.

    A client per call rather than a cached pool. It costs one TCP connect
    per probe, which at one probe every ten seconds is free, and it buys
    the property that matters: the probe proves a connection can be
    *established now*, which is what a newly scheduled worker will need to
    do. A pooled probe can pass on a socket opened before the outage.

    `rediss://` in REDIS_URL gets TLS from redis-py with no change here —
    that is the Phase 4.1-owed "TLS to Redis" leg, configured in the URL.
    """
    client = redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=_PROBE_TIMEOUT_SECONDS,
        socket_timeout=_PROBE_TIMEOUT_SECONDS,
    )
    try:
        client.ping()
    finally:
        client.close()


@router.get("/health", summary="Liveness — is the process answering?")
def liveness(response: Response) -> dict[str, str]:
    """Checks nothing but its own ability to produce a response.

    The shape is unchanged from the inline `/health` this replaced
    (Phase 0), because a probe path and payload that shift under a
    refactor un-monitor a deployment quietly.
    """
    # SecurityHeadersMiddleware only stamps `no-store` under /api/v1, and
    # these two paths are deliberately outside it. A cached probe response
    # is worse than no probe: an intermediary that holds a 200 for even a
    # few seconds keeps routing traffic to an instance that has already
    # said it cannot serve it.
    response.headers["cache-control"] = "no-store"
    return {"status": _OK, "environment": settings.environment}


@router.get("/ready", summary="Readiness — can this instance serve traffic?")
def readiness(response: Response, db: Session = Depends(get_db)) -> dict[str, object]:
    """503 when Postgres or Redis is unreachable; 200 with a per-check
    breakdown otherwise.

    **Object storage is deliberately not checked here**, and that is a
    decision rather than an omission (decision 0036). Three reasons:

    1. *It would convert a partial outage into a total one.* With object
       storage down, uploads and audio playback fail — but consent
       capture, the worklist, patient matching, note review and signing
       all still work, and P0-2's offline queue is built precisely so a
       doctor keeps recording through it. Pulling every instance out of
       the load balancer would take those working paths down too.
    2. *The check is slow and remote.* `HeadBucket` is a network round
       trip to a possibly off-site endpoint, wrapped in botocore's retry
       and backoff. Paying it every ten seconds risks the probe exceeding
       its own timeout and taking the instance out of traffic *because the
       check was slow*, which is the failure in (1) arriving by accident.
    3. *A `HeadBucket` proves little.* What matters is whether a presigned
       multipart round trip works, and that is verified once per deploy by
       the smoke step in docs/runbooks/deployment.md — stronger evidence,
       paid once instead of continuously.

    Object-storage reachability is a monitoring and alerting concern
    (Phase 5.2), not a traffic gate.
    """
    response.headers["cache-control"] = "no-store"

    checks: dict[str, str] = {}

    try:
        check_database(db)
        checks["database"] = _OK
    except Exception:  # noqa: BLE001 - any failure to reach Postgres is "not ready", whatever its class
        # exc_info to the log, never to the body — see the module docstring.
        logger.warning("Readiness: database check failed", exc_info=True)
        checks["database"] = _ERROR
        # The session is shared with the caller for the rest of the
        # request (and, in tests, with the fixture), and a failed
        # statement leaves it in an aborted transaction where every
        # subsequent statement raises InFailedSqlTransaction. Rolling back
        # here keeps the failure local to the probe.
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - already reporting not-ready; a failed rollback adds nothing
            logger.debug("Readiness: rollback after failed database check also failed", exc_info=True)

    try:
        check_redis()
        checks["redis"] = _OK
    except Exception:  # noqa: BLE001 - as above: unreachable is unreachable
        logger.warning("Readiness: redis check failed", exc_info=True)
        checks["redis"] = _ERROR

    ready = all(result == _OK for result in checks.values())
    if not ready:
        # 503, not 500: this is "ask again shortly", and it is the status
        # every orchestrator and load balancer already treats as
        # temporarily-out-of-rotation rather than as a bug.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if ready else "not_ready", "checks": checks}
