"""Phase 5.2: structured logging, correlation IDs, metrics — and above all,
the proof that PHI cannot reach a log line or an error report.

The first block of tests is the point of the phase. Everything else in 5.2
is instrumentation; this is a control, and decision 0034's rule applies to
it exactly — *an untested control is a hope.* Two of the tests below plant
the two artifacts this system actually leaked once already
(docs/progress/4.0-groq-note-generation.md): a patient's name and a verbatim
transcript sentence, first into a log record and then into an exception, and
assert neither survives to the emitted bytes.
"""

from __future__ import annotations

import io
import json
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import metrics
from app.core.observability import (
    CORRELATION_HEADER,
    LOGGABLE_FIELDS,
    PHISafeJSONFormatter,
    PHIScrubbingFilter,
    _before_send,
    configure_logging,
    correlation_scope,
    current_correlation_id,
    init_error_tracking,
    log_event,
    new_correlation_id,
    redact_text,
    register_sensitive,
    safe_exception_summary,
    sanitize_correlation_id,
    scrub_value,
    sensitive_scope,
    stage_timer,
)
from app.core.config import get_settings
from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.models.encounter import Encounter, EncounterPipelineStatus
from app.services.asr.base import TranscriptSegment, TranscriptWord
from app.services.note_generation.base import GeneratedNote, GeneratedSection
from app.services.transcripts import persist_transcript

# The two artifacts this phase exists to keep out of logs. Both are
# realistic rather than obviously-fake: the name is short enough that no
# length rule could ever catch it, and the transcript line is Taglish, which
# is what this clinic's recordings actually sound like.
PATIENT_NAME = "Maria Consuelo Santos"
TRANSCRIPT_LINE = "Doc, sobrang sakit ng tiyan ko simula noong isang linggo, tapos nagsusuka ako"
NOTE_PROSE = (
    "Assessment: acute gastritis, likely NSAID-associated given the patient's reported "
    "self-medication with mefenamic acid over the past week. Differential includes peptic "
    "ulcer disease; red-flag symptoms were absent on questioning. Plan: omeprazole 20mg OD "
    "for fourteen days, stop all NSAIDs, return immediately for black stools or vomiting blood."
)


@contextmanager
def capturing(name: str = "tests.observability"):
    """A logger wired exactly as production is — same filter, same
    formatter — writing to a buffer instead of stdout.

    The record *factory* is global and already installed (app/main.py calls
    `configure_logging` at import, and conftest imports it), which is the
    property being relied on: these tests do not have to opt into scrubbing,
    and neither does any other code in the process.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(PHISafeJSONFormatter())
    handler.addFilter(PHIScrubbingFilter())
    logger = logging.getLogger(name)
    previous = logger.handlers
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        yield logger, stream
    finally:
        logger.handlers = previous


# ===========================================================================
# The control: PHI cannot reach the emitted output
# ===========================================================================


def test_a_patient_name_planted_in_a_log_message_never_reaches_the_output():
    """The leak shape no length rule and no pattern can catch.

    "Maria Consuelo Santos" is twenty-one characters of ordinary prose; it
    is textually indistinguishable from "Connection refused by peer". The
    only thing that can recognise it is *identity* — the process is holding
    that exact string — which is what `sensitive_scope` supplies.
    """
    with capturing() as (logger, stream), sensitive_scope(PATIENT_NAME):
        logger.info("could not match patient %s in the chart", PATIENT_NAME)
        logger.warning(f"failed while linking {PATIENT_NAME}")  # noqa: G004 - the careless call, on purpose

    emitted = stream.getvalue()
    assert PATIENT_NAME not in emitted
    assert "Santos" not in emitted
    assert emitted.count("<redacted-phi>") == 2


def test_a_transcript_line_planted_in_an_exception_never_reaches_the_output():
    """The Phase 4.0 leak, reproduced end to end.

    The exception carries a verbatim consultation sentence in its message —
    exactly what `_extract_tool_input` did — and it is logged with
    `exc_info=True`, so the transcript would appear twice: once in the
    message and once in the rendered traceback. `record.exc_text` is
    pre-rendered by the scrubber for that second copy, which is the one an
    ordinary `logging.Filter` would miss entirely.
    """
    with capturing() as (logger, stream), sensitive_scope(TRANSCRIPT_LINE):
        try:
            raise RuntimeError(f"ASR post-processing failed on: {TRANSCRIPT_LINE}")
        except RuntimeError:
            logger.exception("transcription stage failed")

    emitted = stream.getvalue()
    assert TRANSCRIPT_LINE not in emitted
    assert "sobrang sakit" not in emitted
    # The useful half survives: type, file, and the fact that it happened.
    assert "RuntimeError" in emitted
    assert "Traceback" in emitted


def test_unregistered_clinical_prose_is_replaced_by_a_digest_not_truncated():
    """The backstop, for PHI nobody registered.

    Truncation is not a smaller version of the leak — the first 200
    characters of a generated Assessment is still a generated Assessment —
    so over-length free text is replaced outright. The digest survives so
    two occurrences of the same error can still be recognised as the same
    error during an incident.
    """
    with capturing() as (logger, stream):
        logger.error(f"note generation returned: {NOTE_PROSE}")  # noqa: G004 - the careless call, on purpose

    emitted = stream.getvalue()
    assert "gastritis" not in emitted
    assert "omeprazole" not in emitted
    assert re.search(r"<redacted \d+ chars sha256:[0-9a-f]{8}>", emitted)


def test_a_field_nobody_allow_listed_is_dropped_by_name():
    """`extra=` is the other way text reaches a log line, and the formatter
    *assembles* from `LOGGABLE_FIELDS` rather than filtering a denylist —
    so a field nobody thought of is absent by default, and its name (never
    its value) is reported so the developer sees where it went.
    """
    with capturing() as (logger, stream):
        logger.info("stage done", extra={"transcript": TRANSCRIPT_LINE, "encounter_id": "enc-1"})

    emitted = stream.getvalue()
    assert TRANSCRIPT_LINE not in emitted
    assert '"encounter_id": "enc-1"' in emitted
    assert '"dropped_fields": ["transcript"]' in emitted


def test_percent_style_args_keep_identifiers_and_scrub_prose():
    """Shape decides. A whitespace-free token is an identifier and is what
    logs are *for*; anything with whitespace is prose and gets the full
    treatment. This is what lets every pre-existing `logger.warning("...%s",
    key)` in the codebase keep working unchanged.
    """
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    with capturing() as (logger, stream):
        logger.warning("could not delete audio object %s", f"audio/{uuid}.opus")
        logger.warning("vendor said %s", NOTE_PROSE)

    emitted = stream.getvalue()
    assert uuid in emitted  # a UUID's 12-digit tail must survive the digit rule
    assert "gastritis" not in emitted


def test_a_log_line_cannot_forge_a_second_log_line():
    """Log injection. Newlines and control characters in anything
    interpolated are flattened, so a value cannot close the record and open
    a fabricated one — the classic way an attacker edits the record of
    their own visit.
    """
    with capturing() as (logger, stream):
        logger.info("login failed for %s", 'x\n{"level": "INFO", "message": "admin logged in"}')

    emitted = stream.getvalue()
    records = [json.loads(line) for line in emitted.splitlines() if line.strip()]
    # One record in, one record out. The forged fragment survives as inert
    # text inside a JSON string; what it cannot do is become a second line.
    assert len(records) == 1
    assert chr(10) not in records[0]["message"]


def test_stored_exception_summaries_are_scrubbed_too():
    """`Encounter.last_pipeline_error` is an unencrypted column, and
    `safe_exception_summary` is the only thing that writes it now.
    """
    with sensitive_scope(NOTE_PROSE):
        summary = safe_exception_summary(RuntimeError(f"bad response: {NOTE_PROSE}"))
    assert "gastritis" not in summary
    assert summary.startswith("RuntimeError:")
    assert len(summary) <= 500


def test_no_source_file_logs_an_f_string():
    """The compensating control for the one gap the runtime scrubber
    cannot close.

    A short, unregistered f-string — `logger.info(f"patient {name}")` — is
    shape-indistinguishable from an infrastructure message, so no scrubber
    can catch it. What *can* be caught is the construct itself: with `%`
    args the template is a source literal and only the values are variable,
    which is the property `scrub_record` relies on. So the codebase bans
    f-strings in log calls and this test is the ban.

    It is a lint rule in test's clothing, deliberately: ruff's own G004
    is not in this repo's selected rule set (see ruff.toml on why the
    selection is deliberately narrow), and a rule that only lives in a
    style guide is the kind of rule Phase 4.0 already showed nobody
    remembers.
    """
    pattern = re.compile(r"""\.(debug|info|warning|warn|error|exception|critical|log)\(\s*f["']""")
    offenders = [
        f"{path}:{number}"
        for path in Path(__file__).resolve().parent.parent.joinpath("app").rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]
    assert offenders == [], f"f-string log calls bypass the template scrubber: {offenders}"


def test_registered_values_do_not_leak_between_scopes():
    """A Celery prefork worker runs thousands of tasks in one process and
    one context. If a registration outlived its scope, one patient's PHI
    would still be registered while the next patient's task ran — harmless
    in effect, but an unbounded in-memory PHI store, which is a worse
    problem than the one it solves.
    """
    with sensitive_scope():
        register_sensitive(PATIENT_NAME)
        assert redact_text(f"hello {PATIENT_NAME}") == "hello <redacted-phi>"
    assert redact_text(f"hello {PATIENT_NAME}") == f"hello {PATIENT_NAME}"


def test_scrub_value_leaves_measurements_alone():
    """Numbers must survive as numbers, or every metric becomes a string."""
    assert scrub_value(42) == 42
    assert scrub_value(0.5) == 0.5
    assert scrub_value(None) is None
    assert scrub_value(True) is True
    assert scrub_value("doc@example.com") == "<redacted-email>"


# ===========================================================================
# Sentry: what it captures by default is the problem
# ===========================================================================


def test_error_tracking_is_absent_without_a_dsn_and_does_not_raise():
    """The normal state in development and in this very test run. An API
    that will not boot without an error tracker has turned observability
    into an availability risk.
    """
    assert init_error_tracking(get_settings()) is False


def test_before_send_removes_everything_sentry_captures_by_default():
    """Four defaults, each of which would ship PHI.

    `request` carries the body of `POST /patients/match` (a patient name)
    and `POST /notes/{id}/sections` (clinical prose). `breadcrumbs` carry
    every SQL statement the SQLAlchemy integration sees and every INFO log
    the logging integration sees. `extra` is every non-standard LogRecord
    attribute. And **frame locals** — `include_local_variables` defaults to
    True — mean a stack frame inside `generate_note` ships the entire
    transcript.
    """
    event = {
        "request": {"data": {"family_name": PATIENT_NAME}, "cookies": {"remedy_refresh": "..."}},
        "breadcrumbs": [{"message": TRANSCRIPT_LINE}],
        "extra": {"transcript": TRANSCRIPT_LINE, "encounter_id": "enc-1"},
        "message": f"note generation failed: {NOTE_PROSE}",
        "logentry": {"message": f"failed for {PATIENT_NAME}", "params": [PATIENT_NAME]},
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": f"bad response: {NOTE_PROSE}",
                    "stacktrace": {"frames": [{"function": "generate_note", "vars": {"transcript": TRANSCRIPT_LINE}}]},
                }
            ]
        },
    }

    with sensitive_scope(PATIENT_NAME, TRANSCRIPT_LINE):
        scrubbed = _before_send(event, {})

    assert scrubbed is not None
    serialized = repr(scrubbed)
    assert PATIENT_NAME not in serialized
    assert TRANSCRIPT_LINE not in serialized
    assert "gastritis" not in serialized
    assert "request" not in scrubbed
    assert "breadcrumbs" not in scrubbed
    assert scrubbed["extra"] == {"encounter_id": "enc-1"}
    assert "params" not in scrubbed["logentry"]
    assert "vars" not in scrubbed["exception"]["values"][0]["stacktrace"]["frames"][0]


def test_before_send_tags_the_event_with_the_correlation_id():
    """So an error report and the request that produced it can be joined."""
    with correlation_scope("req-abc123"):
        event = _before_send({"message": "boom"}, {})
    assert event is not None
    assert event["tags"]["correlation_id"] == "req-abc123"


# ===========================================================================
# Correlation IDs
# ===========================================================================


def test_an_inbound_correlation_id_is_echoed_and_a_hostile_one_is_replaced(client):
    """The header is attacker-controlled text destined for a log line."""
    good = client.get("/health", headers={CORRELATION_HEADER: "req-from-the-browser"})
    assert good.headers[CORRELATION_HEADER] == "req-from-the-browser"

    hostile = client.get("/health", headers={CORRELATION_HEADER: 'x\n{"forged": true}'})
    assert hostile.headers[CORRELATION_HEADER] != 'x\n{"forged": true}'
    assert hostile.headers[CORRELATION_HEADER].startswith("req-")


def test_sanitize_rejects_rather_than_cleans():
    assert sanitize_correlation_id("req-abc") == "req-abc"
    assert sanitize_correlation_id("a" * 65) is None
    assert sanitize_correlation_id("has space") is None
    assert sanitize_correlation_id("bad\nvalue") is None


@dataclass
class _RecordedSignature:
    name: str
    args: tuple
    kwargs: dict


class _FakeChain:
    def __init__(self, signatures):
        self.signatures = signatures

    def apply_async(self, *args, **kwargs):
        return None


class _FakeSignature:
    def __init__(self, recorder, name, args, kwargs):
        recorder.append(_RecordedSignature(name, args, kwargs))

    def __or__(self, other):
        return _FakeChain([self, other])


class _FakeTask:
    def __init__(self, recorder, name):
        self._recorder = recorder
        self._name = name

    def s(self, *args, **kwargs):
        return _FakeSignature(self._recorder, self._name, args, kwargs)

    def apply_async(self, args=None, kwargs=None):
        self._recorder.append(_RecordedSignature(self._name, tuple(args or ()), dict(kwargs or {})))


def test_the_correlation_id_crosses_the_celery_boundary_explicitly(monkeypatch):
    """5.2's 📚, as a test. A ContextVar does not survive serialisation into
    Redis, so the ID has to be written into both task signatures — and
    `generate_note`'s has to be a kwarg, because the chain supplies its
    positional argument from the previous task's return value.
    """
    from app.tasks import pipeline

    recorded: list[_RecordedSignature] = []
    monkeypatch.setattr(pipeline, "transcribe_encounter", _FakeTask(recorded, "transcribe"))
    monkeypatch.setattr(pipeline, "generate_note", _FakeTask(recorded, "generate"))

    with correlation_scope("req-deadbeef"):
        pipeline.run_pipeline("enc-1")

    assert [s.name for s in recorded] == ["transcribe", "generate"]
    assert recorded[0].args == ("enc-1",)
    assert recorded[0].kwargs == {"correlation_id": "req-deadbeef"}
    assert recorded[1].args == ()  # supplied by the chain
    assert recorded[1].kwargs == {"correlation_id": "req-deadbeef"}


def test_retrying_note_generation_carries_the_ambient_correlation_id(monkeypatch):
    from app.tasks import pipeline

    recorded: list[_RecordedSignature] = []
    monkeypatch.setattr(pipeline, "generate_note", _FakeTask(recorded, "generate"))

    with correlation_scope("req-retry"):
        pipeline.run_note_generation("enc-2")

    assert recorded[0].args == ("enc-2",)
    assert recorded[0].kwargs == {"correlation_id": "req-retry"}


def test_a_beat_sweep_mints_its_own_run_id_and_hands_it_to_what_it_rekicks(db, monkeypatch):
    """What correlation *means* for a job with no inbound request.

    A sweep has nothing to inherit, and leaving the field blank would make
    the one part of the pipeline nobody triggered also the one part nobody
    can trace. So it mints a run ID and every encounter it re-kicks inherits
    it — which answers a question a request ID cannot: what else did this
    same sweep run touch?
    """
    from app.tasks import pipeline

    seen: list[str | None] = []
    monkeypatch.setattr(pipeline, "run_pipeline", lambda encounter_id: seen.append(current_correlation_id()))

    clinician = Clinician(email="sweep@example.com", full_name="Dr. Cruz", hashed_password="x", role="doctor")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    stale = datetime.now(timezone.utc) - timedelta(hours=4)
    db.add(
        Encounter(
            clinician_id=clinician.id,
            upload_idempotency_key="idem-sweep-corr",
            pipeline_status=EncounterPipelineStatus.UPLOADED,
            pipeline_updated_at=stale,
        )
    )
    db.commit()

    assert pipeline.sweep_stuck_encounters() == 1
    assert len(seen) == 1
    assert seen[0] is not None and seen[0].startswith("sweep-stuck-")
    # And the run ID does not survive the sweep, so the next thing this
    # worker process does is not misattributed to it.
    assert current_correlation_id() is None


def test_new_correlation_ids_say_where_they_came_from():
    assert new_correlation_id("sweep-retention").startswith("sweep-retention-")
    assert sanitize_correlation_id(new_correlation_id("monitor")) is not None


# ===========================================================================
# The pipeline, instrumented
# ===========================================================================


def _seed_encounter(db) -> Encounter:
    clinician = Clinician(email="obs@example.com", full_name="Dr. Lim", hashed_password="x", role="doctor")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key="idem-obs")
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    db.add(
        ConsentLedgerEntry(
            encounter_id=encounter.id,
            event="given",
            participant_roster="[]",
            purposes="[]",
            script_language="en",
        )
    )
    db.commit()
    return encounter


def test_a_vendor_exception_quoting_the_transcript_never_reaches_the_database(db, monkeypatch):
    """The Phase 4.0 bug, as an end-to-end regression test.

    `Encounter.last_pipeline_error` is a plain unencrypted `String(500)`.
    A generator that quotes the transcript back in its exception message —
    which is precisely what a model response missing its tool block looks
    like — used to have that quote written into the column verbatim, where
    it would sit until the row was deleted.

    Note the message is well under the length rule's cap: only the
    registration `generate_note` performs before calling the generator can
    catch this one.
    """
    from app.tasks.pipeline import generate_note

    class _QuotesTheTranscript:
        def generate(self, transcript):
            raise RuntimeError(f"vendor rejected input: {TRANSCRIPT_LINE}")

    monkeypatch.setattr("app.tasks.pipeline.get_note_generator", lambda: _QuotesTheTranscript())

    encounter = _seed_encounter(db)
    persist_transcript(
        db,
        encounter.id,
        provider_name="groq_whisper_large_v3",
        segments=[
            TranscriptSegment(
                speaker="speaker_0",
                words=[
                    TranscriptWord(
                        text=word, start_ms=i * 400, end_ms=i * 400 + 350, confidence=0.9, speaker="speaker_0"
                    )
                    for i, word in enumerate(TRANSCRIPT_LINE.split())
                ],
            )
        ],
    )
    encounter.pipeline_status = EncounterPipelineStatus.TRANSCRIBED
    db.add(encounter)
    db.commit()

    assert generate_note.apply(args=[encounter.id]).state == "FAILURE"

    db.refresh(encounter)
    assert encounter.pipeline_status == EncounterPipelineStatus.GENERATION_FAILED
    assert TRANSCRIPT_LINE not in encounter.last_pipeline_error
    assert "sobrang sakit" not in encounter.last_pipeline_error
    # Still diagnostic: the operator learns what failed and where.
    assert encounter.last_pipeline_error.startswith("RuntimeError:")
    assert "vendor rejected input" in encounter.last_pipeline_error


def test_a_stage_emits_its_latency_even_when_it_fails():
    """Latency for the failed runs is what separates "the vendor is slow"
    from "the API key is missing", and an instrument that only records
    successes cannot tell those apart.
    """
    finished: list[tuple[int, str]] = []
    with capturing() as (logger, stream):
        with pytest.raises(ValueError):
            with stage_timer(
                logger, "pipeline.stage.generate", stage="generate", on_finish=lambda ms, s: finished.append((ms, s))
            ):
                raise ValueError("no api key")

    emitted = stream.getvalue()
    assert '"status": "failed"' in emitted
    assert '"duration_ms"' in emitted
    assert finished and finished[0][1] == "failed"


def test_a_consent_withdrawal_is_not_counted_as_a_stage_failure():
    """P0-1 working correctly must not fire the failure-rate alert."""
    with capturing() as (logger, stream):
        with stage_timer(logger, "pipeline.stage.transcribe", stage="transcribe") as timing:
            timing["status"] = "blocked_no_consent"
    assert '"status": "blocked_no_consent"' in stream.getvalue()


# ===========================================================================
# Metrics, cost and alert rules
# ===========================================================================


class _FakeRedis:
    """Enough Redis for the sample/heartbeat store. A fake rather than a
    real container because what is being tested is this module's own
    arithmetic and idempotency, not redis-py.
    """

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list] = {}

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = str(value)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def expire(self, key, seconds):
        return True

    def set(self, key, value, ex=None):
        self.strings[key] = value

    def get(self, key):
        return self.strings.get(key)

    def llen(self, key):
        return len(self.lists.get(key, []))


@pytest.fixture()
def fake_redis(monkeypatch):
    client = _FakeRedis()
    monkeypatch.setattr(metrics, "_redis_client", lambda: client)
    return client


def test_a_redelivered_task_does_not_double_count_its_cost(fake_redis):
    """Both pipeline tasks are idempotent and can be redelivered
    (`task_acks_late`). A cost store that appended would inflate the day's
    total for a consultation that only happened once, so samples are keyed
    by encounter — idempotent in the same way the pipeline is.
    """
    metrics.record_sample(metrics.SERIES_CONSULT_USD, "enc-1", 0.06)
    metrics.record_sample(metrics.SERIES_CONSULT_USD, "enc-1", 0.06)
    metrics.record_sample(metrics.SERIES_CONSULT_USD, "enc-2", 0.09)
    assert sorted(metrics.read_samples(metrics.SERIES_CONSULT_USD, days=1) or []) == [0.06, 0.09]


def test_an_unreadable_broker_is_unknown_not_zero(monkeypatch):
    """The same distinction decision 0030 drew between `expired` and
    `unreachable`. A queue depth we could not read reported as zero turns
    a broker outage — the exact incident this monitoring exists to catch —
    into a clean bill of health.
    """
    monkeypatch.setattr(metrics, "_redis_client", lambda: None)
    assert metrics.queue_depth() is None
    assert metrics.heartbeat_age_seconds(metrics.HEARTBEAT_STUCK_SWEEP) is None
    assert metrics.read_samples(metrics.SERIES_CONSULT_USD) is None


def test_a_heartbeat_that_cannot_be_read_alerts_the_same_as_one_that_is_stale(fake_redis):
    """ "No evidence it ran" and "evidence it did not run" call for the same
    phone call — and for the retention sweep in particular, the failure is
    silent, lawful-looking and cumulative.
    """
    stale = _snapshot(heartbeats={metrics.HEARTBEAT_STUCK_SWEEP: 9999.0, metrics.HEARTBEAT_RETENTION_SWEEP: 1.0})
    unknown = _snapshot(heartbeats={metrics.HEARTBEAT_STUCK_SWEEP: 1.0, metrics.HEARTBEAT_RETENTION_SWEEP: None})

    for snapshot in (stale, unknown):
        rules = [alert.rule for alert in metrics.evaluate_alerts(snapshot)]
        assert "scheduled_job_stalled" in rules


def _snapshot(**overrides) -> metrics.HealthSnapshot:
    base = {
        "taken_at": datetime.now(timezone.utc),
        "window_hours": 24,
        "stages": {"transcribe": metrics.StageHealth("transcribe", 20, 0)},
        "stuck_encounters": 0,
        "queue_depth": 0,
        "uploads_started": 20,
        "uploads_incomplete": 0,
        "heartbeats": {metrics.HEARTBEAT_STUCK_SWEEP: 60.0, metrics.HEARTBEAT_RETENTION_SWEEP: 60.0},
        "cost_samples": [],
    }
    base.update(overrides)
    return metrics.HealthSnapshot(**base)


def test_a_healthy_system_fires_nothing():
    assert metrics.evaluate_alerts(_snapshot()) == []


def test_one_failure_on_a_quiet_morning_is_not_a_hundred_percent_failure_rate():
    """Without a minimum sample size the first failure of a quiet morning
    is a 100% failure rate, the alert fires, and within a week everybody
    has learned to ignore it — strictly worse than no alert, because it
    also trains them to ignore the next one.
    """
    quiet = _snapshot(stages={"generate": metrics.StageHealth("generate", 0, 1)})
    assert metrics.evaluate_alerts(quiet) == []

    real = _snapshot(stages={"generate": metrics.StageHealth("generate", 5, 5)})
    assert [a.rule for a in metrics.evaluate_alerts(real)] == ["pipeline_failure_rate"]


def test_each_alert_the_checklist_asks_for_can_fire():
    fired = {
        alert.rule
        for alert in metrics.evaluate_alerts(
            _snapshot(
                stages={"generate": metrics.StageHealth("generate", 1, 9)},
                stuck_encounters=40,
                queue_depth=500,
                uploads_started=20,
                uploads_incomplete=12,
                cost_samples=[0.2, 0.3, 0.4],
            )
        )
    }
    assert fired == {
        "pipeline_failure_rate",
        "stuck_encounters",
        "queue_depth",
        "upload_failure_rate",
        "cost_per_consult_over_target",
    }


def test_a_critical_alert_is_emitted_at_error_so_sentry_can_turn_it_into_an_issue():
    """The whole delivery mechanism, in one assertion. `critical` maps to
    ERROR because that is the level Sentry's logging integration converts
    into an issue; without a DSN these remain lines in a log file, which
    the runbook says in as many words.
    """
    with capturing("app.core.metrics") as (_logger, stream):
        metrics.emit_alerts(
            [
                metrics.Alert(rule="stuck_encounters", severity="critical", value=40, threshold=3),
                metrics.Alert(rule="queue_depth", severity="warning", value=500, threshold=20),
            ]
        )
    emitted = stream.getvalue()
    assert '"level": "ERROR"' in emitted
    assert '"level": "WARNING"' in emitted
    assert '"rule": "stuck_encounters"' in emitted


def test_transcription_owns_the_consult_budget_not_note_generation():
    """The finding the cost dashboard exists to produce.

    Both legs go to Groq, but ASR is billed per hour of audio and note
    generation per token — and at published rates the audio leg is more
    than an order of magnitude larger. That makes the PRD's <$0.10 target
    effectively a *duration* budget, which is a useful thing to know before
    the pilot rather than after the first invoice.
    """
    thirty_minutes = metrics.estimate_consult_cost(
        audio_seconds=30 * 60,
        transcript_chars=60_000,
        note_chars=2_000,
    )
    assert thirty_minutes.total_usd < get_settings().cost_target_usd_per_consult
    assert thirty_minutes.asr_usd > 10 * thirty_minutes.note_usd
    assert thirty_minutes.estimated is True

    ninety_minutes = metrics.estimate_consult_cost(
        audio_seconds=90 * 60,
        transcript_chars=180_000,
        note_chars=2_500,
    )
    assert ninety_minutes.total_usd > get_settings().cost_target_usd_per_consult


def test_percentiles_name_an_observation_that_actually_happened():
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert metrics.percentile(values, 0.5) == 3.0
    assert metrics.percentile(values, 0.95) == 100.0
    assert metrics.percentile([], 0.5) is None


def test_the_snapshot_counts_failures_and_stuck_work_from_the_database(db, fake_redis):
    clinician = Clinician(email="snap@example.com", full_name="Dr. Yu", hashed_password="x", role="doctor")
    db.add(clinician)
    db.commit()
    db.refresh(clinician)

    now = datetime.now(timezone.utc)
    rows = [
        (EncounterPipelineStatus.NOTE_GENERATED, now - timedelta(minutes=5)),
        (EncounterPipelineStatus.NOTE_GENERATED, now - timedelta(minutes=6)),
        (EncounterPipelineStatus.GENERATION_FAILED, now - timedelta(minutes=7)),
        (EncounterPipelineStatus.UPLOADED, now - timedelta(hours=3)),
    ]
    for index, (status, stamped) in enumerate(rows):
        db.add(
            Encounter(
                clinician_id=clinician.id,
                upload_idempotency_key=f"idem-snap-{index}",
                pipeline_status=status,
                pipeline_updated_at=stamped,
            )
        )
    db.commit()

    snapshot = metrics.collect_snapshot(db, now=now)
    assert snapshot.stages["generate"].succeeded == 2
    assert snapshot.stages["generate"].failed == 1
    assert snapshot.stuck_encounters == 1
    assert snapshot.queue_depth == 0


def test_the_report_renders_without_a_broker(db, monkeypatch):
    """The dashboard has to be readable on a VM where Redis is the thing
    that just broke.
    """
    monkeypatch.setattr(metrics, "_redis_client", lambda: None)
    report = metrics.render_report(db)
    assert "COST (estimated" in report
    assert "unknown (broker unreachable)" in report
    assert "ALERTS" in report


# ===========================================================================
# Configuration
# ===========================================================================


def test_configure_logging_is_idempotent_and_does_not_nest_the_scrubber():
    """Called from app/main.py at import and from Celery's setup_logging
    signal. Re-wrapping the record factory each time would work (scrubbing
    is idempotent) but would stack a call frame per invocation.
    """
    configure_logging(get_settings(), force=True)
    configure_logging(get_settings(), force=True)
    factory = logging.getLogRecordFactory()
    assert getattr(factory, "_remedy_factory", False) is True
    assert getattr(getattr(factory, "_remedy_base"), "_remedy_factory", False) is False


def test_every_field_this_codebase_logs_is_allow_listed():
    """A field emitted through `log_event` that nobody allow-listed is
    silently dropped, which is safe but confusing. This catches the typo at
    build time instead.
    """
    with capturing() as (logger, stream):
        log_event(logger, "pipeline.stage.generate", encounter_id="enc-1", duration_ms=12, stage="generate")
    assert '"dropped_fields"' not in stream.getvalue()
    assert {"encounter_id", "duration_ms", "stage", "event", "correlation_id"} <= LOGGABLE_FIELDS | {"correlation_id"}


def test_generated_note_sections_are_registered_before_they_can_be_logged(db, monkeypatch):
    """The second half of the Phase 4.0 leak: the *output* is PHI too, and
    an exception raised after generation (a database error, say) can quote
    it just as easily as one raised during.
    """
    from app.tasks.pipeline import generate_note

    section = GeneratedSection(text=NOTE_PROSE)

    class _GeneratesThenTheDatabaseFails:
        def generate(self, transcript):
            return GeneratedNote(
                assessment=section,
                plan=section,
                subjective=section,
                objective=section,
                provider="groq",
                prompt_version="groq-v1",
            )

    monkeypatch.setattr("app.tasks.pipeline.get_note_generator", lambda: _GeneratesThenTheDatabaseFails())
    monkeypatch.setattr(
        "app.tasks.pipeline.Note",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError(f"could not persist: {NOTE_PROSE}")),
    )

    encounter = _seed_encounter(db)
    persist_transcript(
        db,
        encounter.id,
        provider_name="groq_whisper_large_v3",
        segments=[
            TranscriptSegment(
                speaker="speaker_0",
                words=[TranscriptWord(text="Ano", start_ms=0, end_ms=200, confidence=0.9, speaker="speaker_0")],
            )
        ],
    )
    encounter.pipeline_status = EncounterPipelineStatus.TRANSCRIBED
    db.add(encounter)
    db.commit()

    assert generate_note.apply(args=[encounter.id]).state == "FAILURE"
    db.refresh(encounter)
    assert "gastritis" not in encounter.last_pipeline_error
    assert "omeprazole" not in encounter.last_pipeline_error


# ---------------------------------------------------------------------------
# Sentry: the options that must be off *before* it is pointed at production
# ---------------------------------------------------------------------------


class _FakeSentrySDK:
    """Stands in for the real SDK, which is not installed here.

    A fake is the right instrument for this specific question. What is being
    tested is not sentry-sdk's behaviour — it is *which options this codebase
    passes*, which is the thing that has to be right before a DSN ever exists
    and the thing nobody can check afterwards without leaking to find out.
    """

    def __init__(self, reject: set[str] | None = None):
        self.kwargs: dict = {}
        self.reject = reject or set()
        self.captured: list = []

    def init(self, **kwargs):
        rejected = self.reject & set(kwargs)
        if rejected:
            # What an older or newer major version does with an option it
            # does not know.
            raise TypeError(f"unexpected keyword argument {sorted(rejected)[0]!r}")
        self.kwargs = kwargs

    def capture_exception(self, exc):
        self.captured.append(exc)


@contextmanager
def _fake_sentry(monkeypatch, sdk: _FakeSentrySDK):
    import sys

    import app.core.observability as observability

    monkeypatch.setitem(sys.modules, "sentry_sdk", sdk)
    monkeypatch.setattr(observability, "_sentry_initialised", False)
    yield sdk


def test_sentry_is_initialised_with_stack_frame_locals_off(monkeypatch):
    """**The sneakiest version of this leak.**

    `include_local_variables` defaults to **True** in sentry-sdk. A stack
    frame inside `generate_note` holds `transcript` — every segment of a
    verbatim consultation — and a frame inside `_build_section` holds the
    generated note. So an ordinary, unrelated crash anywhere down that call
    stack would ship the entire consultation to a third-party service, in a
    field nobody looks at, from code that never mentions logging.

    Nothing about the exception has to be PHI-related for this to happen,
    which is why it cannot be fixed by being careful about exception
    messages. It is fixed here, once, at init.
    """
    sdk = _FakeSentrySDK()
    settings = get_settings().model_copy(update={"sentry_dsn": "https://key@example.invalid/1"})

    with _fake_sentry(monkeypatch, sdk):
        assert init_error_tracking(settings) is True

    assert sdk.kwargs["include_local_variables"] is False
    # The other three defaults that capture PHI.
    assert sdk.kwargs["max_request_body_size"] == "never"  # request bodies carry names and clinical prose
    assert sdk.kwargs["max_breadcrumbs"] == 0  # SQL statements and every INFO log line
    assert sdk.kwargs["send_default_pii"] is False
    # And the transport hook, so the guarantee does not rest on init alone.
    assert sdk.kwargs["before_send"] is not None
    assert sdk.kwargs["before_breadcrumb"]({"message": TRANSCRIPT_LINE}, {}) is None


def test_an_sdk_that_rejects_a_safety_option_gets_no_dsn_at_all(monkeypatch):
    """Fails closed, which is the whole point.

    If a future SDK renames `include_local_variables` — it has been renamed
    once already, from `with_locals` — the alternative to this is a Sentry
    that initialises successfully with frame locals back at their default.
    That is strictly worse than no error tracking, so "start without the
    safety options" is not one of the available outcomes.
    """
    sdk = _FakeSentrySDK(reject={"include_local_variables"})
    settings = get_settings().model_copy(update={"sentry_dsn": "https://key@example.invalid/1"})

    with _fake_sentry(monkeypatch, sdk):
        assert init_error_tracking(settings) is False

    assert sdk.kwargs == {}
