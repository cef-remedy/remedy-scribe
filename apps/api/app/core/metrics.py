"""Pipeline metrics, alert rules, and the per-consult cost dashboard
(Phase 5.2, P0-8).

## What this is, and what it deliberately is not

There is no Prometheus, no StatsD and no Grafana in this stack, and 5.2 is
not the phase to add three. The pilot is **one clinic on one VM**; a
metrics stack there costs more to keep alive than the thing it watches, and
an alerting pipeline nobody has provisioned an on-call rotation for is
theatre. So the honest shape is:

* **Numbers are emitted as structured log events** (`observability.log_event`
  with `metric` / `value` / `unit`), which means they inherit the PHI
  guarantees of the logging boundary for free and land wherever the logs
  already land. A log shipper can turn them into a time series later
  without a single call-site change — that is the whole reason they are
  events with stable names rather than `logger.info` sentences.
* **Samples are kept in Redis**, which is already in the stack as the
  Celery broker, so the report below can show percentiles rather than a
  single instantaneous reading. No new table and therefore no migration.
  The trade is stated plainly: Redis is not durable here, so a flush loses
  cost history. The durable version needs a column and a migration (see
  the module's open follow-ups in docs/progress/5.2-observability.md).
* **Alert *rules* are evaluated here; alert *delivery* is not.**
  `evaluate_alerts` produces breaches, `monitor_pipeline_health`
  (app/tasks/pipeline.py) emits them at WARNING/ERROR, and an ERROR record
  becomes a Sentry issue — which is a real notification path only when a
  DSN is configured. With no DSN, these are lines in a log file that
  nothing reads. That is not a metaphor for alerting; it is a prerequisite
  for it, and docs/runbooks/observability.md says exactly which piece is
  still owed and by whom.

## Cost

The PRD's target is **<$0.10 per consult**. Real cost needs the token
counts the vendors return, and neither `services/asr/groq.py` nor
`services/note_generation/groq.py` reads the `usage` block today — so what
this module computes is an **estimate**, labelled as one everywhere it
appears. The estimate is not a guess about pricing (those are published
per-unit rates, in settings, changeable when the invoice disagrees); it is
a guess about *token counts*, derived from character counts the pipeline
already holds in memory. Characters-per-token is the weak link, and it is
weakest exactly where this product lives: Taglish tokenises worse than
English on a byte-pair vocabulary trained mostly on English.

The estimate is still worth having, because it answers the question that
actually matters before the pilot — **which leg of the pipeline owns the
budget** — and the answer is not close. See the progress note.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.observability import log_event
from app.models.encounter import Encounter, EncounterPipelineStatus

logger = logging.getLogger(__name__)

#: The queue Celery publishes to with the default routing config. Queue
#: depth is `LLEN` on this key — Celery stores a plain Redis list per
#: queue, so this is a fact about the broker rather than an estimate.
CELERY_DEFAULT_QUEUE = "celery"

#: Namespace for everything this module writes to Redis, so an operator
#: reading `KEYS remedy:obs:*` can see the whole of it and `DEL` it without
#: touching the broker's own keys.
_REDIS_PREFIX = "remedy:obs"

#: Sample series names. Stable strings, because they are what a later
#: dashboard would query.
SERIES_TRANSCRIBE_MS = "latency.transcribe_ms"
SERIES_GENERATE_MS = "latency.generate_ms"
SERIES_CONSULT_USD = "cost.consult_usd"

#: Heartbeat names, one per scheduled job that nothing else watches.
HEARTBEAT_STUCK_SWEEP = "sweep_stuck_encounters"
HEARTBEAT_RETENTION_SWEEP = "sweep_expired_retention"
HEARTBEAT_MONITOR = "monitor_pipeline_health"

#: Samples are kept for a quarter — long enough to answer "did this get
#: worse after the model change?" and short enough that the broker's memory
#: is not a growing liability. Nothing here is PHI (encounter IDs are
#: already Celery task arguments in this same Redis), but unbounded
#: retention of anything is a smell.
_SAMPLE_TTL_SECONDS = 90 * 24 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _redis_client() -> Any | None:
    """A short-timeout Redis client, or None if Redis is unavailable.

    Every caller treats None as "this reading is unknown", never as zero.
    That distinction is the same one `grounding._audio_state` draws between
    `expired` and `unreachable` (decision 0030): a queue depth we could not
    read is not a queue depth of nought, and reporting it as one turns a
    broker outage — the exact incident this monitoring exists to catch —
    into a clean bill of health.
    """
    try:
        import redis
    except ImportError:  # pragma: no cover - redis is a hard dependency of celery
        return None
    try:
        return redis.Redis.from_url(
            get_settings().redis_url,
            socket_timeout=2,
            socket_connect_timeout=2,
            decode_responses=True,
        )
    except Exception:  # noqa: BLE001 - a malformed URL must not break the caller
        return None


def _sample_key(series: str, day: date) -> str:
    return f"{_REDIS_PREFIX}:sample:{series}:{day.isoformat()}"


def record_sample(series: str, key: str, value: float, *, now: datetime | None = None) -> bool:
    """Record one observation, keyed so a re-run overwrites rather than
    double-counts.

    A hash field per encounter, not a list append: both Celery tasks are
    idempotent and can be redelivered (`task_acks_late`), and a retried
    note generation appending a second cost sample would inflate the daily
    total for a consultation that only happened once. Keying by encounter
    makes the store idempotent in the same way the pipeline is.

    Returns whether it was written, so a caller can tell "recorded" from
    "Redis was down" without catching.
    """
    client = _redis_client()
    if client is None:
        return False
    day = (now or _utcnow()).date()
    try:
        redis_key = _sample_key(series, day)
        client.hset(redis_key, key, value)
        client.expire(redis_key, _SAMPLE_TTL_SECONDS)
        return True
    except Exception:  # noqa: BLE001 - metrics must never break the work they measure
        logger.debug("Could not record a metric sample", exc_info=True)
        return False


def read_samples(series: str, *, days: int = 7, now: datetime | None = None) -> list[float] | None:
    """Every observation in the last `days`, or None if Redis is unreachable."""
    client = _redis_client()
    if client is None:
        return None
    today = (now or _utcnow()).date()
    values: list[float] = []
    try:
        for offset in range(days):
            for raw in client.hgetall(_sample_key(series, today - timedelta(days=offset))).values():
                try:
                    values.append(float(raw))
                except (TypeError, ValueError):
                    continue
        return values
    except Exception:  # noqa: BLE001
        logger.debug("Could not read metric samples", exc_info=True)
        return None


def record_heartbeat(name: str, *, now: datetime | None = None) -> bool:
    """Stamp "this scheduled job just finished a run".

    The gap Phase 4 closed the *code* for and left the *watching* of open:
    `sweep_stuck_encounters` and `sweep_expired_retention` are the two jobs
    that exist because nothing was watching, and until now nothing was
    watching them either. A Beat process that dies takes both with it
    silently — the app keeps serving, notes keep generating, and the only
    symptom is that stuck encounters stop being rescued and expired PHI
    stops being deleted. Neither has a user who complains.
    """
    client = _redis_client()
    if client is None:
        return False
    try:
        client.set(f"{_REDIS_PREFIX}:heartbeat:{name}", (now or _utcnow()).isoformat(), ex=_SAMPLE_TTL_SECONDS)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("Could not record a heartbeat", exc_info=True)
        return False


def heartbeat_age_seconds(name: str, *, now: datetime | None = None) -> float | None:
    """Seconds since that job last finished; None if unknown.

    None covers three cases that are genuinely different — Redis down, the
    job has never run, the key expired — and all three mean the same thing
    to the alert rule: *we cannot show that this job is running.* Which is
    what gets alerted on, because "no evidence it ran" and "evidence it did
    not run" call for the same phone call.
    """
    client = _redis_client()
    if client is None:
        return None
    try:
        raw = client.get(f"{_REDIS_PREFIX}:heartbeat:{name}")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        stamped = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    return max(0.0, ((now or _utcnow()) - stamped).total_seconds())


def queue_depth(queue: str = CELERY_DEFAULT_QUEUE) -> int | None:
    """Messages waiting for a worker, or None if the broker cannot be read."""
    client = _redis_client()
    if client is None:
        return None
    try:
        return int(client.llen(queue))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsultCost:
    """An estimated cost breakdown for one consultation.

    `estimated=True` is a field rather than a docstring caveat because this
    value is destined for a dashboard someone will quote in a budget
    conversation, and the number and the caveat must not be separable.
    """

    audio_seconds: float
    transcript_chars: int
    note_chars: int
    tokens_in: int
    tokens_out: int
    asr_usd: float
    note_usd: float
    estimated: bool = True

    @property
    def total_usd(self) -> float:
        return self.asr_usd + self.note_usd


def estimate_consult_cost(
    *,
    audio_seconds: float,
    transcript_chars: int,
    note_chars: int,
) -> ConsultCost:
    """Estimate one consultation's vendor cost from what the pipeline
    already holds.

    Both legs go to Groq (decisions 0018 and 0035), which prices ASR per
    hour of **audio** and note generation per **token**. So the two legs
    scale on completely different axes, and the interesting consequence
    falls out of the arithmetic rather than out of an opinion: transcription
    is charged by the length of the consultation and generation is charged
    by roughly the same thing, but the per-unit rates differ by two orders
    of magnitude. See docs/progress/5.2-observability.md for the break-even
    minute this implies.

    No API call and no PHI leaves this function: it takes counts, not text.
    """
    settings = get_settings()
    chars_per_token = max(1.0, settings.cost_chars_per_token)

    tokens_in = int(settings.cost_prompt_overhead_tokens + transcript_chars / chars_per_token)
    tokens_out = int(note_chars / chars_per_token)

    asr_usd = (audio_seconds / 3600.0) * settings.cost_asr_usd_per_audio_hour
    note_usd = (tokens_in / 1_000_000.0) * settings.cost_note_usd_per_million_input_tokens + (
        tokens_out / 1_000_000.0
    ) * settings.cost_note_usd_per_million_output_tokens

    return ConsultCost(
        audio_seconds=audio_seconds,
        transcript_chars=transcript_chars,
        note_chars=note_chars,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        asr_usd=round(asr_usd, 6),
        note_usd=round(note_usd, 6),
    )


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile. No numpy, and no interpolation: with a
    pilot's worth of samples an interpolated p95 is a fiction about data
    that does not exist, while nearest-rank always names an observation
    that actually happened.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


@dataclass
class StageHealth:
    stage: str
    succeeded: int
    failed: int

    @property
    def sample_size(self) -> int:
        return self.succeeded + self.failed

    @property
    def failure_rate(self) -> float | None:
        return (self.failed / self.sample_size) if self.sample_size else None


@dataclass
class HealthSnapshot:
    """Everything the alert rules and the dashboard read, gathered once.

    One snapshot rather than each rule running its own query: the rules are
    evaluated together and reported together, and a set of "current"
    numbers taken seconds apart from each other describes a system that
    never existed at any single moment.
    """

    taken_at: datetime
    window_hours: int
    stages: dict[str, StageHealth]
    stuck_encounters: int
    queue_depth: int | None
    uploads_started: int
    uploads_incomplete: int
    heartbeats: dict[str, float | None]
    cost_samples: list[float] | None
    latency_ms: dict[str, list[float] | None] = field(default_factory=dict)

    @property
    def upload_failure_rate(self) -> float | None:
        return (self.uploads_incomplete / self.uploads_started) if self.uploads_started else None


def collect_snapshot(db: Session, *, now: datetime | None = None) -> HealthSnapshot:
    """Read the current state of the pipeline from the database and broker.

    **Per-stage success/failure is inferred from terminal status**, not
    from a per-attempt event table, because no such table exists. An
    encounter that reached `note_generated` is counted as a success for
    both stages; one sitting in `transcription_failed` is a transcription
    failure. The approximation is worth naming: an encounter that failed
    transcription three times, was retried by a human and then succeeded
    shows up here purely as a success. That understates the failure rate,
    which is the wrong direction to be wrong in — so the per-attempt truth
    is emitted separately as `pipeline.stage.*` events from the tasks
    themselves, and this snapshot is the cheap always-available view.
    """
    settings = get_settings()
    now = now or _utcnow()
    since = now - timedelta(hours=settings.metrics_window_hours)

    def _count(*conditions: Any) -> int:
        return int(db.query(func.count(Encounter.id)).filter(*conditions).scalar() or 0)

    recent = Encounter.pipeline_updated_at >= since

    transcribed_or_better = _count(
        recent,
        Encounter.pipeline_status.in_((EncounterPipelineStatus.TRANSCRIBED, EncounterPipelineStatus.NOTE_GENERATED)),
    )
    generated = _count(recent, Encounter.pipeline_status == EncounterPipelineStatus.NOTE_GENERATED)
    transcription_failed = _count(recent, Encounter.pipeline_status == EncounterPipelineStatus.TRANSCRIPTION_FAILED)
    generation_failed = _count(recent, Encounter.pipeline_status == EncounterPipelineStatus.GENERATION_FAILED)

    stuck_threshold = now - timedelta(minutes=settings.pipeline_stuck_threshold_minutes)
    stuck = _count(
        Encounter.pipeline_status.in_((EncounterPipelineStatus.UPLOADED, EncounterPipelineStatus.TRANSCRIBED)),
        Encounter.pipeline_updated_at < stuck_threshold,
    )

    # An upload failure leaves no row state of its own (see
    # EncounterPipelineStatus' comment: the upload path is synchronous and
    # retries client-side). What it *does* leave is an encounter that was
    # created, never confirmed an upload, and then stopped changing — which
    # is exactly what a recording whose upload never completed looks like
    # from the server. Named `uploads_incomplete` rather than "failed"
    # because the client may still be offline with the audio safely in
    # IndexedDB (P0-2), and calling that a failure would be a lie.
    upload_stall = now - timedelta(minutes=settings.alert_upload_stall_minutes)
    uploads_started = _count(Encounter.created_at >= since)
    uploads_incomplete = _count(
        Encounter.created_at >= since,
        Encounter.created_at < upload_stall,
        Encounter.pipeline_status == EncounterPipelineStatus.RECORDING,
        Encounter.audio_object_key.is_(None),
    )

    return HealthSnapshot(
        taken_at=now,
        window_hours=settings.metrics_window_hours,
        stages={
            "transcribe": StageHealth("transcribe", transcribed_or_better, transcription_failed),
            "generate": StageHealth("generate", generated, generation_failed),
        },
        stuck_encounters=stuck,
        queue_depth=queue_depth(),
        uploads_started=uploads_started,
        uploads_incomplete=uploads_incomplete,
        heartbeats={
            HEARTBEAT_STUCK_SWEEP: heartbeat_age_seconds(HEARTBEAT_STUCK_SWEEP, now=now),
            HEARTBEAT_RETENTION_SWEEP: heartbeat_age_seconds(HEARTBEAT_RETENTION_SWEEP, now=now),
        },
        cost_samples=read_samples(SERIES_CONSULT_USD, days=7, now=now),
        latency_ms={
            "transcribe": read_samples(SERIES_TRANSCRIBE_MS, days=7, now=now),
            "generate": read_samples(SERIES_GENERATE_MS, days=7, now=now),
        },
    )


# ---------------------------------------------------------------------------
# Alert rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    """One breached rule. `value` and `threshold` travel with it because an
    alert that says "queue depth is high" and not "queue depth is 412,
    threshold 20" makes the reader open a second tool before they can even
    decide whether to care.
    """

    rule: str
    severity: str  # "warning" | "critical"
    value: float | None
    threshold: float
    fields: dict[str, Any] = field(default_factory=dict)


def evaluate_alerts(snapshot: HealthSnapshot) -> list[Alert]:
    """The four rules 5.2 asks for, plus the one Phase 4 left owing.

    Every rate rule carries a minimum sample size. Without it the first
    failed encounter of a quiet morning is a 100% failure rate, the alert
    fires, and within a week everybody has learned to ignore it — which is
    strictly worse than having no alert, because it also trains people to
    ignore the next one.
    """
    settings = get_settings()
    alerts: list[Alert] = []

    for stage, health in snapshot.stages.items():
        rate = health.failure_rate
        if rate is None or health.sample_size < settings.alert_pipeline_min_sample:
            continue
        if rate > settings.alert_pipeline_failure_rate:
            alerts.append(
                Alert(
                    rule="pipeline_failure_rate",
                    severity="critical",
                    value=round(rate, 4),
                    threshold=settings.alert_pipeline_failure_rate,
                    fields={"stage": stage, "sample_size": health.sample_size},
                )
            )

    if snapshot.stuck_encounters > settings.alert_stuck_encounters:
        # Stuck work is *supposed* to be self-healing: sweep_stuck_encounters
        # re-kicks it every 5 minutes. So a standing population of stuck
        # encounters does not mean "work is queued", it means the rescue
        # itself is not working — a second-order failure nothing else
        # reports.
        alerts.append(
            Alert(
                rule="stuck_encounters",
                severity="critical",
                value=snapshot.stuck_encounters,
                threshold=settings.alert_stuck_encounters,
            )
        )

    if snapshot.queue_depth is None:
        alerts.append(
            Alert(
                rule="queue_depth_unreadable",
                severity="critical",
                value=None,
                threshold=0,
                fields={"queue": CELERY_DEFAULT_QUEUE},
            )
        )
    elif snapshot.queue_depth > settings.alert_queue_depth:
        alerts.append(
            Alert(
                rule="queue_depth",
                severity="warning",
                value=snapshot.queue_depth,
                threshold=settings.alert_queue_depth,
                fields={"queue": CELERY_DEFAULT_QUEUE},
            )
        )

    upload_rate = snapshot.upload_failure_rate
    if (
        upload_rate is not None
        and snapshot.uploads_started >= settings.alert_pipeline_min_sample
        and upload_rate > settings.alert_upload_failure_rate
    ):
        alerts.append(
            Alert(
                rule="upload_failure_rate",
                severity="warning",
                value=round(upload_rate, 4),
                threshold=settings.alert_upload_failure_rate,
                fields={"sample_size": snapshot.uploads_started},
            )
        )

    for name, max_minutes in (
        (HEARTBEAT_STUCK_SWEEP, settings.alert_stuck_sweep_max_age_minutes),
        (HEARTBEAT_RETENTION_SWEEP, settings.alert_retention_sweep_max_age_minutes),
    ):
        age = snapshot.heartbeats.get(name)
        if age is None or age > max_minutes * 60:
            # Critical for the retention sweep in particular: it is the
            # only thing deleting derived PHI on schedule (decision 0033),
            # and its failure is silent, lawful-looking and cumulative.
            alerts.append(
                Alert(
                    rule="scheduled_job_stalled",
                    severity="critical",
                    value=None if age is None else round(age, 1),
                    threshold=max_minutes * 60,
                    fields={"task_name": name},
                )
            )

    p95_cost = percentile(snapshot.cost_samples or [], 0.95)
    if p95_cost is not None and p95_cost > settings.cost_target_usd_per_consult:
        alerts.append(
            Alert(
                rule="cost_per_consult_over_target",
                severity="warning",
                value=round(p95_cost, 4),
                threshold=settings.cost_target_usd_per_consult,
                fields={"sample_size": len(snapshot.cost_samples or [])},
            )
        )

    return alerts


def emit_snapshot(snapshot: HealthSnapshot) -> None:
    """Publish the snapshot as metric events, one per number.

    One event per metric rather than one fat event: this is the shape every
    log-to-metric pipeline expects, and it means adding a metric never
    changes the schema of an existing one.
    """
    for stage, health in snapshot.stages.items():
        _metric("pipeline.stage.succeeded", health.succeeded, "count", stage=stage)
        _metric("pipeline.stage.failed", health.failed, "count", stage=stage)
        if health.failure_rate is not None:
            _metric(
                "pipeline.stage.failure_rate",
                round(health.failure_rate, 4),
                "ratio",
                stage=stage,
                sample_size=health.sample_size,
            )
    _metric("pipeline.encounters.stuck", snapshot.stuck_encounters, "count")
    if snapshot.queue_depth is not None:
        _metric("queue.depth", snapshot.queue_depth, "count", queue=CELERY_DEFAULT_QUEUE)
    _metric("uploads.started", snapshot.uploads_started, "count", window_hours=snapshot.window_hours)
    _metric("uploads.incomplete", snapshot.uploads_incomplete, "count", window_hours=snapshot.window_hours)
    for name, age in snapshot.heartbeats.items():
        if age is not None:
            _metric("scheduled_job.age", round(age, 1), "seconds", task_name=name)

    samples = snapshot.cost_samples or []
    if samples:
        for label, fraction in (("p50", 0.5), ("p95", 0.95)):
            value = percentile(samples, fraction)
            if value is not None:
                _metric(f"cost.consult_usd.{label}", round(value, 5), "usd", sample_size=len(samples))


def emit_alerts(alerts: list[Alert]) -> None:
    """Emit each breach at a level that means something downstream.

    `critical` goes out at ERROR specifically because Sentry's logging
    integration turns an ERROR record into an issue, and an issue is the
    only thing in this stack that can actually wake someone. `warning` at
    WARNING stays in the log for the daily read. That mapping is the entire
    delivery mechanism, and it only exists when SENTRY_DSN is set — see the
    runbook.
    """
    for alert in alerts:
        log_event(
            logger,
            "alert.firing",
            level=logging.ERROR if alert.severity == "critical" else logging.WARNING,
            rule=alert.rule,
            severity=alert.severity,
            value=alert.value,
            threshold=alert.threshold,
            breach=True,
            **alert.fields,
        )


def _metric(name: str, value: float, unit: str, **fields: Any) -> None:
    log_event(logger, "metric", metric=name, value=value, unit=unit, **fields)


# ---------------------------------------------------------------------------
# The dashboard
# ---------------------------------------------------------------------------


def render_report(db: Session, *, now: datetime | None = None) -> str:
    """A text dashboard, printable from the VM the pilot runs on.

    `python -m app.core.metrics` from `apps/api/`. Not a web page: a chart
    server is one more thing to secure on a box that holds PHI, and the
    audience for this is one operator on one machine who is already
    SSH'd in. When there is a log pipeline, the same numbers are already
    being emitted as events and the page can be built there.
    """
    settings = get_settings()
    snapshot = collect_snapshot(db, now=now)
    alerts = evaluate_alerts(snapshot)
    lines: list[str] = []

    def row(label: str, value: object) -> None:
        lines.append(f"  {label:<34} {value}")

    lines.append(f"Remedy Scribe — observability report ({snapshot.taken_at.isoformat(timespec='seconds')})")
    lines.append(f"Environment: {settings.environment}   window: last {snapshot.window_hours}h")
    lines.append("")
    lines.append("PIPELINE")
    for stage, health in snapshot.stages.items():
        rate = health.failure_rate
        rate_text = "n/a (no completions in window)" if rate is None else f"{rate:.1%} of {health.sample_size}"
        row(f"{stage}: failure rate", rate_text)
        samples = snapshot.latency_ms.get(stage)
        if samples is None:
            row(f"{stage}: latency", "unknown (broker unreachable)")
        elif samples:
            p50 = percentile(samples, 0.5) or 0
            p95 = percentile(samples, 0.95) or 0
            row(f"{stage}: latency p50/p95", f"{p50 / 1000:.1f}s / {p95 / 1000:.1f}s over {len(samples)} runs")
        else:
            row(f"{stage}: latency", "no samples in the last 7 days")
    row("stuck encounters", snapshot.stuck_encounters)
    row("queue depth", "unknown (broker unreachable)" if snapshot.queue_depth is None else snapshot.queue_depth)

    lines.append("")
    lines.append("UPLOADS")
    row("started in window", snapshot.uploads_started)
    upload_rate = snapshot.upload_failure_rate
    row(
        "not confirmed after stall window",
        f"{snapshot.uploads_incomplete}" + ("" if upload_rate is None else f" ({upload_rate:.1%})"),
    )

    lines.append("")
    lines.append("SCHEDULED JOBS")
    for name, age in snapshot.heartbeats.items():
        row(name, "never seen / unreadable" if age is None else f"last ran {age / 60:.1f} min ago")

    lines.append("")
    lines.append(f"COST (estimated — target ${settings.cost_target_usd_per_consult:.2f}/consult)")
    samples = snapshot.cost_samples
    if samples is None:
        row("per-consult cost", "unknown (broker unreachable)")
    elif not samples:
        row("per-consult cost", "no consultations costed in the last 7 days")
    else:
        p50 = percentile(samples, 0.5) or 0.0
        p95 = percentile(samples, 0.95) or 0.0
        over = sum(1 for value in samples if value > settings.cost_target_usd_per_consult)
        row("per-consult p50 / p95", f"${p50:.4f} / ${p95:.4f} over {len(samples)} consults")
        row("over target", f"{over} of {len(samples)} ({over / len(samples):.0%})")
    row("rates in use", f"ASR ${settings.cost_asr_usd_per_audio_hour}/audio-hour, note gen per-token")
    lines.append("  NOTE: token counts are estimated from character counts; the vendor")
    lines.append("        `usage` block is not captured yet. See docs/progress/5.2-observability.md.")

    lines.append("")
    lines.append("ALERTS")
    if not alerts:
        lines.append("  none firing")
    for alert in alerts:
        lines.append(
            f"  [{alert.severity.upper()}] {alert.rule}: value={alert.value} threshold={alert.threshold} {alert.fields}"
        )

    return "\n".join(lines)


def main() -> None:  # pragma: no cover - operator entry point
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        print(render_report(db))
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main()
