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
)
