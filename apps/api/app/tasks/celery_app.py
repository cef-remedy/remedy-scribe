import ssl
from urllib.parse import urlparse

from celery import Celery
from celery.signals import setup_logging

from app.core.config import get_settings
from app.core.observability import configure_logging

settings = get_settings()


def _tls_options(redis_url: str) -> dict | None:
    """Kombu's redis transport does not inherit redis-py's TLS defaults.

    `check_redis()` in `routes/health.py` calls `redis.Redis.from_url()`
    directly, which infers TLS from the `rediss://` scheme with no further
    config needed. Kombu (Celery's broker/backend client) does not: it
    refuses to open a `rediss://` connection at all unless a certificate
    policy is stated explicitly — and it refuses *late*, at the first real
    connection attempt rather than at import or at `Celery()` construction.
    That attempt is `chain.apply_async()` inside `complete_upload`
    (`routes/uploads.py`), so the failure mode is a plain 500 on the upload
    route with nothing pointing at Celery, Kombu, or Redis at all.

    `CERT_REQUIRED` rather than `CERT_NONE`: Upstash (and every managed
    Redis this app targets) presents a certificate signed by a public CA,
    so there is no reason to accept an unverified one. Returns `None` for a
    plain `redis://` broker (local dev, docker-compose) — passing an empty
    dict instead of `None` is not equivalent here: older Celery releases
    treat `{}` as "use TLS with defaults", which would force a handshake
    against a socket that was never going to speak TLS.
    """
    if urlparse(redis_url).scheme != "rediss":
        return None
    return {"ssl_cert_reqs": ssl.CERT_REQUIRED}


@setup_logging.connect
def _configure_worker_logging(**_kwargs) -> None:
    """Phase 5.2 (P0-8): install this app's PHI-scrubbing logging in the
    worker and beat processes.

    Connecting to `setup_logging` at all is what stops Celery hijacking the
    root logger — the signal is documented as "if it has a receiver, Celery
    will not configure logging itself". That matters more here than in the
    web process: Celery's own default handlers would emit records our
    formatter never sees, and the worker is where the *transcript* and the
    *generated note* are in memory. The one process that must not have an
    unscrubbed log handler is exactly the one Celery configures for you.
    """
    configure_logging(settings, force=True)


celery_app = Celery(
    "remedy_scribe",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.pipeline", "app.tasks.retention"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_use_ssl=_tls_options(settings.redis_url),
    redis_backend_use_ssl=_tls_options(settings.redis_url),
    # Transcription/note-gen calls are slow, retryable, external — acks_late
    # + a modest prefetch keep a worker crash from silently dropping an
    # in-flight encounter.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Phase 1.5: the "nothing is watching stuck work" half of pipeline
    # failure handling. Dead-lettering (in app/tasks/pipeline.py) only
    # catches a task that actually ran and raised; this catches the one
    # that never ran at all — the broker down, or the worker pool at
    # zero, at the moment run_pipeline fired. Runs independently of
    # PIPELINE_STUCK_THRESHOLD_MINUTES (that setting controls how *old*
    # counts as stuck, not how often we check) — checking every 5
    # minutes against a 30-minute-default threshold means a stuck
    # encounter is caught within minutes of crossing it, not by luck.
    # Requires a `celery -A app.tasks.celery_app beat` process running
    # alongside the worker — see infra/docker-compose.yml's `beat` service.
    # Phase 4.4: retention enforcement (decision 0033). The bucket
    # lifecycle rule already expires the audio *objects* whether or not
    # this app is running; this job exists for what the bucket cannot
    # see — the transcript and note-revision rows Postgres holds, which
    # are derived PHI with the same retention clock.
    #
    # Hourly, not every 5 minutes and not daily. `audio_retention_days`
    # gives the policy day granularity, so an hour of lag is invisible
    # against a 90-day clock — a tighter interval buys nothing. But this
    # job is also the backstop for a withdrawal whose immediate delete
    # failed (object storage briefly unreachable), and P0-1 says "without
    # undue delay"; a nightly cron would turn a patient's withdrawal into
    # an up-to-24-hour wait for the derived rows. An hour is the longest
    # interval that still reads as "without undue delay" and the shortest
    # one the retention policy can actually tell apart.
    #
    # Same beat process as sweep-stuck-encounters (see
    # infra/docker-compose.yml's `beat` service) — beat only schedules,
    # so a second periodic task costs nothing there.
    beat_schedule={
        "sweep-stuck-encounters": {
            "task": "pipeline.sweep_stuck_encounters",
            "schedule": 300.0,
        },
        "sweep-expired-retention": {
            "task": "retention.sweep_expired_retention",
            "schedule": 3600.0,
        },
        # Phase 5.2 (P0-8): the job that watches the two above. Every five
        # minutes, matching the faster of the two sweeps — an alert saying
        # "the stuck-encounter sweep has not run in 20 minutes" is worth
        # very little if the thing that notices only looks every hour.
        #
        # It runs in the same beat process as its subjects, which is a
        # genuine limitation rather than an oversight: if beat dies, the
        # monitor dies with the sweeps it watches and nothing fires. The
        # honest fix is an external check (the deployment runbook's
        # process supervisor, or an uptime ping) and it is written up as
        # such in docs/runbooks/observability.md rather than pretended
        # away here. What this *does* catch is the far more common case —
        # beat alive, a sweep failing or wedged on a query.
        "monitor-pipeline-health": {
            "task": "pipeline.monitor_pipeline_health",
            "schedule": 300.0,
        },
    },
)
