from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "remedy_scribe",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.pipeline"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
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
    beat_schedule={
        "sweep-stuck-encounters": {
            "task": "pipeline.sweep_stuck_encounters",
            "schedule": 300.0,
        },
    },
)
