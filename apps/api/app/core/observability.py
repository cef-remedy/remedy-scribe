"""Structured logging, correlation IDs and error tracking (Phase 5.2, P0-8).

Everything in this module exists to make one property true:

> **A log line or an error report cannot carry PHI by accident.**

That wording is deliberate. This codebase has already been bitten twice by
the accidental case, and neither bite was a careless developer — both were
reasonable code written by someone who had thought about PHI:

* `note_generation/haiku.py:_extract_tool_input` interpolated the *entire*
  Anthropic response into a `RuntimeError` message (see
  docs/progress/4.0-groq-note-generation.md). The exception the pipeline
  then wrote to `Encounter.last_pipeline_error` — an **unencrypted**
  `String(500)` — would have contained generated clinical prose.
* `Encounter.last_pipeline_error`'s own comment argues the column is safe
  because "every exception raised from these two tasks is an
  infrastructure/vendor error ... never something built from transcript or
  note content." That argument was true when it was written, false a phase
  later, and nothing detected the change.

The lesson both times is the same one decision 0032 drew for the audit
trail: **a rule that depends on remembering is not a control.** So none of
what follows is a convention. A denylist of "things not to log" is
explicitly rejected — it protects exactly the leaks someone already
thought of, which are never the ones that happen.

Three enforcement boundaries, each of which a careless call site passes
through whether it wants to or not:

1. **`logging.setLogRecordFactory`** (`configure_logging`). Every record
   created anywhere in the process — this app, uvicorn, celery, boto3,
   httpx — is built by our factory, which rewrites the message from its
   *template* rather than its interpolated form, scrubs the interpolated
   values, and pre-renders a scrubbed traceback. A filter can be bypassed
   by a handler that does not have it; the record factory cannot, because
   there is only one and `Logger.makeRecord` always calls it.
2. **`PHISafeJSONFormatter`** — the emitted line is *assembled* from an
   allow-list (`LOGGABLE_FIELDS`), not copied and cleaned. A field nobody
   allow-listed is not in the output, so `extra={"transcript": ...}`
   emits the field's *name* and nothing else.
3. **`_before_send`** — the Sentry transport. Sentry's defaults are the
   opposite of what a PHI system wants (see `init_error_tracking`), and
   the hook is what makes "configure the scrubbing before pointing it at
   production" a property of the code rather than of a console setting
   somebody has to remember to tick.

The residual gap is stated rather than papered over: a **short** piece of
free text that has not been registered as sensitive (a patient's name in
an f-string, say) is shape-indistinguishable from "Connection refused",
and no scrubber can tell them apart. Two things close it — `sensitive_scope`
where PHI provably lives (`app/tasks/pipeline.py`), and a test that fails
the build if any `logger.*(f"...")` call appears under `app/`. See
docs/decisions/0037 for why that pair is the honest answer and what would
change it.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import secrets
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Correlation IDs
# ---------------------------------------------------------------------------

#: The header the browser sends and the API echoes. Lowercase because ASGI
#: normalises header names to lowercase bytes and comparing anything else
#: is a bug waiting for a client that capitalises differently.
CORRELATION_HEADER = "x-correlation-id"

#: Ambient correlation ID for the current request / task / sweep run.
#:
#: A `ContextVar`, not a thread-local: FastAPI runs request handlers in an
#: asyncio task and Celery's prefork worker runs one task per process, and
#: a ContextVar is correct in both (each asyncio task and each thread gets
#: its own view). It is read as a *fallback* only — the async boundary is
#: crossed by passing the ID explicitly as a task kwarg, because a
#: ContextVar does not survive serialisation into Redis and back. That is
#: the whole point of 5.2's 📚.
_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "remedy_correlation_id",
    default=None,
)

#: An inbound correlation ID is attacker-controlled text that ends up
#: inside log lines. Without this, `X-Correlation-ID: abc\nlevel=INFO
#: msg="admin logged in"` forges a log entry — log injection is the
#: classic way an attacker edits the record of their own visit. Anything
#: not matching is discarded and replaced, never "cleaned up": a
#: half-accepted identifier is worse than a fresh one, because it looks
#: like the client's.
_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def new_correlation_id(origin: str = "req") -> str:
    """A fresh correlation ID, prefixed with where it came from.

    The prefix is not decoration. Phase 5.2 has three genuinely different
    origins — a browser request, a Beat sweep, and a monitor run — and
    "which of these is this trace?" is the first question anyone asks of a
    log line. `secrets` rather than `uuid4` only because 8 bytes of hex is
    shorter to read and paste than a UUID and there is nothing to
    guarantee here beyond non-collision.
    """
    return f"{origin}-{secrets.token_hex(8)}"


def current_correlation_id() -> str | None:
    return _correlation_id.get()


def sanitize_correlation_id(raw: str | None) -> str | None:
    """The inbound value, or None if it is not safely loggable."""
    if raw is None:
        return None
    candidate = raw.strip()
    return candidate if _SAFE_CORRELATION_ID.match(candidate) else None


@contextmanager
def correlation_scope(correlation_id: str | None, *, origin: str = "req") -> Iterator[str]:
    """Bind a correlation ID for the duration of the block, minting one if
    the caller has none. Always resets, so a Celery worker process that
    handles thousands of tasks never leaks one task's ID into the next.
    """
    resolved = sanitize_correlation_id(correlation_id) or new_correlation_id(origin)
    token = _correlation_id.set(resolved)
    try:
        yield resolved
    finally:
        _correlation_id.reset(token)


# ---------------------------------------------------------------------------
# Registered sensitive values
# ---------------------------------------------------------------------------

#: Values known to be PHI *right now*, in this request or task. See
#: `sensitive_scope`.
_sensitive_values: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "remedy_sensitive_values",
    default=(),
)

#: Below this length a "value" is too generic to substitute safely — a
#: three-letter surname substring would redact half of every log line it
#: appears in, including the parts that make the line useful.
_MIN_SENSITIVE_LENGTH = 6

#: A transcript is hundreds of segments and every registered value costs a
#: substring scan per emitted log record. The cap keeps the scrubber's cost
#: bounded on a 40-minute consultation; the values that do not fit are the
#: later, shorter segments, and the length cap in `redact_text` catches
#: prose regardless of whether it was registered.
_MAX_SENSITIVE_VALUES = 250

_REDACTED_PHI = "<redacted-phi>"


@contextmanager
def sensitive_scope(*values: object) -> Iterator[None]:
    """Register PHI strings for the duration of the block, so that if any
    of them reaches a log record or an exception message they are replaced
    rather than emitted.

    This is the answer to the one leak shape a scrubber cannot recognise
    by shape: a patient's name, or a single transcript sentence, is
    textually indistinguishable from an infrastructure error message. It is
    recognisable by *identity* — we are holding the exact string — and this
    is how that knowledge is handed to the logging boundary.

    Scoped rather than global on purpose. A process-wide registry of PHI
    strings is itself a PHI store: it would keep every transcript this
    worker ever saw resident in memory for the life of the process, which
    is a worse problem than the one it solves. Registration lasts exactly
    as long as the code that is holding the PHI anyway.

    Deliberately tolerant of `None` and non-strings so call sites can pass
    optional model attributes without a guard each time.

    **Always establishes a boundary, even with no values**, because that is
    what makes `register_sensitive` safe. A Celery prefork worker runs
    thousands of tasks in one process and one context; without a token to
    reset, values registered mid-task would still be registered when the
    next, unrelated task ran — a slow leak of one patient's PHI into
    another's log scrubbing, which is harmless in effect and exactly the
    kind of unbounded lifetime this scope exists to avoid.
    """
    token = _sensitive_values.set(_merge_sensitive(_sensitive_values.get(), values))
    try:
        yield
    finally:
        _sensitive_values.reset(token)


def register_sensitive(*values: object) -> None:
    """Add values to the innermost `sensitive_scope`.

    Needed because the PHI does not exist yet when the scope has to open. A
    task must open its scope *outside* its own `try`, so that the `except`
    block — where `safe_exception_summary` runs, and where the two
    historical leaks would have surfaced — is still inside it. But the
    transcript is only loaded partway down the `try`. So the scope opens
    empty and this fills it.

    Registrations are discarded when the enclosing scope exits, since that
    scope resets the variable to its own prior value.
    """
    _sensitive_values.set(_merge_sensitive(_sensitive_values.get(), values))


def _merge_sensitive(existing: tuple[str, ...], values: tuple[object, ...]) -> tuple[str, ...]:
    additions = [
        text for value in values if isinstance(value, str) and len(text := value.strip()) >= _MIN_SENSITIVE_LENGTH
    ]
    if not additions:
        return existing
    # Longest first: replacing the whole segment before its sub-phrases
    # means a message containing both is redacted once, not shredded.
    return tuple(sorted({*existing, *additions}, key=len, reverse=True))[:_MAX_SENSITIVE_VALUES]


# ---------------------------------------------------------------------------
# The scrubber
# ---------------------------------------------------------------------------

#: Shapes that are PHI under the Data Privacy Act whenever they appear, so
#: they are removed even from text short enough to pass the length rule.
#:
#: This list is explicitly **not** the mechanism — it is a courtesy. A
#: denylist only ever covers the leaks someone already imagined; the
#: mechanism is the field allow-list plus the length rule below. Adding a
#: pattern here is fine, relying on it is not.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A clinician's or patient's email address is a direct identifier.
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<redacted-email>"),
    # A bare date is very likely a date of birth. An ISO *timestamp* is
    # excluded by the trailing guard (the `T` is a word character), because
    # timestamps belong in the log envelope and are not PHI.
    (re.compile(r"(?<![\w:-])\d{4}-\d{2}-\d{2}(?![\w:-])"), "<redacted-date>"),
    # Phone / PhilHealth / PRC-number shaped runs. Only spaces are allowed
    # as separators and the guards exclude hyphenated hex, so a UUID's
    # 12-digit trailing group survives — encounter IDs are the thing these
    # logs exist to correlate on and mangling them would be self-defeating.
    (re.compile(r"(?<![\w.-])\+?\d[\d ]{6,}\d(?![\w.-])"), "<redacted-digits>"),
)

#: Characters that let a log line pretend to be two log lines.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

#: Whitespace-free tokens made only of these characters are identifiers,
#: object keys, model names, HTTP statuses and URLs — the things logs are
#: *for*. They pass through unaltered (bar the email rule), which is what
#: keeps `logger.warning("Could not delete audio object %s", key)` useful.
_SAFE_SCALAR = re.compile(r"^[A-Za-z0-9_.:+@/#=,()\[\]{}<>|~^$&*!?%'\"-]{0,160}$")


def _escape_controls(text: str) -> str:
    return _CONTROL_CHARS.sub(" ", text.replace("\r\n", " ").replace("\n", " ").replace("\r", " "))


def _digest(text: str) -> str:
    """A short, non-reversible label for text we refuse to emit.

    Borrowed from `config.secret_fingerprint`'s reasoning: an investigator
    needs to know that the error at 10:04 and the error at 10:41 were the
    *same* error, and a digest answers that without the text. Eight hex
    characters is plenty for equality-within-an-incident and far too few to
    attack.
    """
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]


def redact_text(text: str, *, max_chars: int | None = None) -> str:
    """Scrub one unit of free text.

    Order matters. Registered values go first, so a transcript sentence
    inside an otherwise-useful message is replaced in place and the rest of
    the message survives. Pattern redaction next. The length rule last,
    because it is the backstop and it is total.

    **The length rule does not truncate.** Over-length text is replaced
    outright by its length and digest. Truncating a transcript to its first
    200 characters leaves 200 characters of verbatim consultation in the
    log, which is not a smaller version of the problem — it is the problem.
    Prose is the shape all three of this system's large PHI artifacts take
    (transcript, note sections, revision history), so "cannot be said in
    `max_chars`" is the one property that reliably separates them from a
    vendor error string.
    """
    limit = get_settings().log_max_free_text_chars if max_chars is None else max_chars
    cleaned = _escape_controls(text)

    for value in _sensitive_values.get():
        if value in cleaned:
            cleaned = cleaned.replace(value, _REDACTED_PHI)

    for pattern, replacement in _REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)

    if len(cleaned) > limit:
        return f"<redacted {len(cleaned)} chars sha256:{_digest(cleaned)}>"
    return cleaned


def scrub_value(value: object) -> object:
    """Scrub one interpolated log argument or structured field value.

    **Shape decides**, and that is the whole design: a whitespace-free
    token is an identifier and is kept verbatim; anything containing
    whitespace is prose and gets the full `redact_text` treatment. Numbers
    and booleans are never PHI on their own and pass through as themselves
    so a JSON consumer sees numbers rather than strings.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _SAFE_SCALAR.match(value):
            # Still runs the email rule: an address is a perfectly safe
            # *shape* and a direct identifier all the same.
            return _REDACTIONS[0][0].sub(_REDACTIONS[0][1], value)
        return redact_text(value)
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {redact_text(str(value))}"
    if isinstance(value, (list, tuple, set, frozenset)):
        return [scrub_value(item) for item in value]
    if isinstance(value, dict):
        # Keys are developer-chosen literals in practice; values are not.
        return {str(k): scrub_value(v) for k, v in value.items()}
    return redact_text(repr(value))


def safe_exception_summary(exc: BaseException, *, max_chars: int = 480) -> str:
    """`type: scrubbed message`, for the places an exception is *stored*
    rather than logged.

    `app/tasks/pipeline.py:_mark_stage_failure` writes this into
    `Encounter.last_pipeline_error`, an unencrypted `String(500)` column.
    Phase 4.0 found generated clinical prose one bug away from landing
    there; routing the write through the same scrubber as the log line
    means the column's safety no longer rests on every future exception
    author having read its comment.
    """
    summary = f"{type(exc).__name__}: {redact_text(str(exc), max_chars=max_chars)}"
    return summary[:max_chars]


# ---------------------------------------------------------------------------
# The loggable-field allow-list
# ---------------------------------------------------------------------------

#: The complete set of structured fields that may appear in a log line.
#:
#: An allow-list, not a denylist, and that asymmetry is the point: a field
#: nobody thought about is absent from the output instead of present in it.
#: Every entry here is either an opaque identifier (already recorded in
#: `audit_logs`, and meaningless without the database), a measurement, or
#: an enum value. **No entry is or can hold free text**, which is what
#: makes `log_event` PHI-free by construction rather than by review.
#:
#: Adding a field is a deliberate act. Adding one that can hold prose
#: defeats the mechanism, so don't — emit a measurement of the prose
#: instead (`chars`, `segments`) the way the cost estimate does.
LOGGABLE_FIELDS: frozenset[str] = frozenset(
    {
        # identity of the trace
        "correlation_id",
        "event",
        "origin",
        # entities (opaque UUIDs)
        "encounter_id",
        "note_id",
        "patient_id",
        "clinician_id",
        "transcript_id",
        "task_id",
        "task_name",
        # pipeline shape
        "stage",
        "status",
        "pipeline_status",
        "attempt",
        "max_attempts",
        "terminal",
        "provider",
        "model",
        "prompt_version",
        "reason",
        "error_type",
        # measurements
        "duration_ms",
        "count",
        "value",
        "unit",
        "metric",
        "segments",
        "chars",
        "audio_seconds",
        "tokens_in",
        "tokens_out",
        "usd",
        "queue",
        "queue_depth",
        "window_hours",
        "age_seconds",
        # HTTP
        "http_method",
        "http_path",
        "http_status",
        # alerting
        "rule",
        "severity",
        "threshold",
        "breach",
        "sample_size",
        # scrubber self-reporting
        "dropped_fields",
    }
)

#: Attributes every `LogRecord` carries. Used to tell an `extra=` field
#: from stdlib bookkeeping when the formatter assembles a line.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_SCRUBBED_MARKER = "_remedy_phi_scrubbed"
#: Names (never values) of fields the allow-list refused, carried on the
#: record so the formatter can report them.
_DROPPED_MARKER = "_remedy_dropped_fields"


def scrub_record(record: logging.LogRecord) -> logging.LogRecord:
    """Rewrite a log record so that nothing it holds can carry PHI onward.

    Idempotent (via `_SCRUBBED_MARKER`), because it is deliberately
    installed at two boundaries — the record factory and a handler filter —
    and the second must not re-scrub a `<redacted ...>` placeholder into a
    digest of itself.

    **`record.msg` is treated as a template, never as a message.** With
    `%`-style args the template is a literal from the source file and the
    variable part is `record.args`, so scrubbing the args and interpolating
    afterwards preserves every existing log line in this codebase while
    making the interpolated values safe. With no args the string is of
    unknown provenance — it may be an f-string — so the whole thing goes
    through `redact_text`.

    `exc_info` is left in place but a scrubbed traceback is pre-rendered
    into `record.exc_text`. `logging.Formatter` prefers an existing
    `exc_text` and never re-formats, so the raw traceback cannot reach a
    log file — while Sentry, which reads `exc_info` directly, still gets a
    real stack trace (scrubbed again on its own transport).
    """
    if getattr(record, _SCRUBBED_MARKER, False):
        return record

    message: str
    if record.args:
        template = record.msg if isinstance(record.msg, str) else str(record.msg)
        args = record.args
        scrubbed: Any
        if isinstance(args, dict):
            scrubbed = {k: scrub_value(v) for k, v in args.items()}
        else:
            scrubbed = tuple(scrub_value(v) for v in args)
        try:
            message = template % scrubbed
        except Exception:  # noqa: BLE001 - a broken format string must not lose the line
            message = f"{template} <unformattable-args>"
        # Bound the assembled line. The template is trusted, the args are
        # already scrubbed, so this is a volume guard rather than a PHI one.
        message = _escape_controls(message)[: get_settings().log_max_line_chars]
    else:
        message = redact_text(record.msg if isinstance(record.msg, str) else repr(record.msg))

    record.msg = message
    record.args = None

    if record.exc_info and not record.exc_text:
        exc_type, exc_value, exc_tb = record.exc_info
        if exc_type is not None:
            rendered = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            # Line by line: a traceback is mostly file paths and source
            # lines (not PHI, and the useful part), with the exception
            # message at the end (the part that has leaked twice).
            record.exc_text = "\n".join(redact_text(line, max_chars=400) for line in rendered.splitlines())
    if record.stack_info:
        record.stack_info = "\n".join(redact_text(line, max_chars=400) for line in record.stack_info.splitlines())

    if getattr(record, "correlation_id", None) is None:
        record.correlation_id = current_correlation_id()  # type: ignore[attr-defined]

    setattr(record, _SCRUBBED_MARKER, True)
    return record


class PHIScrubbingFilter(logging.Filter):
    """Defence in depth behind the record factory.

    Two jobs the factory cannot do. First, `Logger.makeRecord` applies
    `extra=` keys to the record *after* the factory has run, so only a
    filter can see them — this is where a stray `extra={"transcript": ...}`
    is caught rather than merely omitted by the formatter. Second, a
    `LogRecord` constructed directly (some libraries do) never passes
    through the factory at all.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        scrub_record(record)
        dropped: list[str] = list(getattr(record, _DROPPED_MARKER, []))
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            if key not in LOGGABLE_FIELDS:
                # Removed, not blanked. Anything downstream that harvests
                # `record.__dict__` — Sentry's logging integration does
                # exactly that — must not find the value still sitting
                # there; and the field's *name* is kept so a developer can
                # see where their field went instead of assuming it arrived.
                record.__dict__.pop(key, None)
                dropped.append(key)
                continue
            record.__dict__[key] = scrub_value(value)
        if dropped:
            setattr(record, _DROPPED_MARKER, sorted(set(dropped)))
        return True


class PHISafeJSONFormatter(logging.Formatter):
    """Assembles the emitted line from `LOGGABLE_FIELDS`.

    Assembles, not filters. The difference matters: a formatter that
    serialised `record.__dict__` and removed known-bad keys would emit
    whatever the next `extra=` introduces. This one can only emit fields
    that were written down here, so the default for anything new is
    silence — and the field's *name* is reported in `dropped_fields`, so a
    developer sees that their field went nowhere instead of assuming it
    arrived.
    """

    def format(self, record: logging.LogRecord) -> str:
        scrub_record(record)
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        dropped: list[str] = list(getattr(record, _DROPPED_MARKER, []))
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_") or value is None:
                continue
            if key in LOGGABLE_FIELDS:
                payload[key] = scrub_value(value)
            else:
                dropped.append(key)
        if dropped:
            payload["dropped_fields"] = sorted(set(dropped))

        if record.exc_text:
            payload["exception"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info

        # default=str so an unexpected object type degrades to its repr
        # rather than raising inside logging (a logging failure during an
        # incident is the worst possible time to lose the log).
        return json.dumps(payload, default=str, ensure_ascii=False)


class PHISafeTextFormatter(logging.Formatter):
    """The same content, human-readable, for a developer's terminal.

    Same scrubbing, because a dev machine holding a real transcript is
    exactly the machine that later gets a screenshot pasted into a chat.
    """

    def format(self, record: logging.LogRecord) -> str:
        scrub_record(record)
        head = f"{self.formatTime(record)} {record.levelname:<7} {record.name}"
        correlation = getattr(record, "correlation_id", None)
        if correlation:
            head = f"{head} [{correlation}]"
        fields = " ".join(
            f"{k}={scrub_value(v)}"
            for k, v in sorted(record.__dict__.items())
            if k in LOGGABLE_FIELDS and k != "correlation_id" and v is not None
        )
        line = f"{head} {record.getMessage()}"
        if fields:
            line = f"{line} | {fields}"
        if record.exc_text:
            line = f"{line}\n{record.exc_text}"
        return line


# ---------------------------------------------------------------------------
# Emitting
# ---------------------------------------------------------------------------


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: object,
) -> None:
    """The sanctioned way for this codebase to log.

    An `event` is a stable dotted identifier (`pipeline.stage.finished`),
    not a sentence: it is what you group by, and a sentence with an ID
    interpolated into it cannot be grouped. Everything variable travels as
    allow-listed structured fields, so **there is no free text on this
    path at all** — which is the strongest form of the guarantee this
    module exists for, and the reason 5.2's own instrumentation uses it
    exclusively.
    """
    logger.log(level, "%s", event, extra={"event": event, **fields}, exc_info=exc_info)


@contextmanager
def stage_timer(
    logger: logging.Logger,
    event: str,
    *,
    on_finish: Callable[[int, str], None] | None = None,
    **fields: object,
) -> Iterator[dict[str, object]]:
    """Time a block and emit its duration, whether it succeeds or raises.

    The `finally` is the load-bearing part. Latency for the *failed* runs
    is what tells you whether a stage is failing fast (a bad API key) or
    slow (a vendor timing out), and an instrument that only records
    successes cannot distinguish those — which is precisely the question
    5.2's 📚 says correlation and timing exist to answer.

    Yields a mutable dict so the block can attach facts it only learns on
    the way (segment counts, token estimates) to the same emitted line, and
    can override `status` — a transcription stopped by a consent withdrawal
    did not fail, and recording it as a failure would put a legitimate,
    correct outcome into the failure-rate alert.

    `on_finish(duration_ms, status)` is the hook for recording the same
    measurement somewhere other than the log — a callback rather than an
    import of `app.core.metrics`, which would be circular (metrics logs
    through this module) and would tie the logging boundary to a storage
    backend it has no business knowing about.
    """
    extra: dict[str, object] = dict(fields)
    started = time.perf_counter()
    failed = False
    try:
        yield extra
    except BaseException:
        failed = True
        raise
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        payload: dict[str, Any] = {"duration_ms": duration_ms, "status": "failed" if failed else "ok"}
        payload.update(extra)
        log_event(logger, event, level=logging.WARNING if failed else logging.INFO, **payload)
        if on_finish is not None:
            try:
                on_finish(duration_ms, str(payload["status"]))
            except Exception:  # noqa: BLE001 - a metric sink must not raise into the timed block
                logger.debug("stage_timer on_finish hook failed", exc_info=True)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Loggers that ship their own handlers and would otherwise bypass ours.
#: Uvicorn installs handlers on these when started from its CLI; emptying
#: them and re-enabling propagation funnels every line through the root
#: handler this module owns. `uvicorn.access` is the one that matters most
#: — its default format prints the raw request line, query string included.
_HIJACKED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "celery", "celery.app.trace")

_configured = False


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Install the record factory, the root handler, and the formatter.

    Idempotent: called from `app/main.py` at import time and from Celery's
    `setup_logging` signal, and in tests. Re-installing the record factory
    twice would nest the scrubber inside itself — harmless, since
    `scrub_record` is idempotent, but pointless.
    """
    global _configured
    if _configured and not force:
        return

    settings = settings or get_settings()

    # The factory, not a filter: `Logger.makeRecord` always calls it, so
    # there is no handler configuration and no third-party library that can
    # route around it. This is the single unbypassable boundary.
    previous_factory = logging.getLogRecordFactory()
    if getattr(previous_factory, "_remedy_factory", False):
        base_factory = getattr(previous_factory, "_remedy_base", previous_factory)
    else:
        base_factory = previous_factory

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return scrub_record(base_factory(*args, **kwargs))

    factory._remedy_factory = True  # type: ignore[attr-defined]
    factory._remedy_base = base_factory  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(PHISafeJSONFormatter() if settings.log_format == "json" else PHISafeTextFormatter())
    handler.addFilter(PHIScrubbingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    for name in _HIJACKED_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    _configured = True


# ---------------------------------------------------------------------------
# Error tracking
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

#: Set once `sentry_sdk.init` has actually been called, so a second call
#: (worker + web process share this module) is a no-op and so tests can
#: assert on the outcome without a DSN.
_sentry_initialised = False


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Scrub a Sentry event on its way out of the process.

    Sentry's job is to capture *context*, and context is where PHI lives.
    Four specific things are removed, each of which is on by default in the
    SDK or produced by an integration:

    * `request` — headers, cookies and body. `POST /patients/match` carries
      a patient's name in its body and `POST /notes/{id}/sections` carries
      clinical prose. `max_request_body_size="never"` is set at init as
      well; this is the belt to that braces, because the init kwarg is one
      SDK upgrade away from being renamed again (it already was once).
    * `extra` — rebuilt from `LOGGABLE_FIELDS`. The logging integration
      copies every non-standard `LogRecord` attribute into here.
    * `breadcrumbs` — dropped wholesale. The SQLAlchemy integration records
      executed statements, and a statement's parameters include the
      ciphertext *and* the plaintext search hashes; the logging integration
      records every INFO line. Auditing each breadcrumb type on every SDK
      upgrade is not a control anyone will keep performing, so the whole
      channel is off. `max_breadcrumbs=0` at init says the same thing.
    * every `exception.value` and frame — scrubbed and stripped of locals.
      Locals are the sneaky one: a stack frame inside `generate_note` holds
      the entire transcript, and `include_local_variables` defaults to
      **True**.
    """
    event.pop("request", None)
    event.pop("breadcrumbs", None)

    extra = event.get("extra")
    if isinstance(extra, dict):
        event["extra"] = {k: scrub_value(v) for k, v in extra.items() if k in LOGGABLE_FIELDS}

    if isinstance(event.get("message"), str):
        event["message"] = redact_text(event["message"])

    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        if isinstance(logentry.get("message"), str):
            logentry["message"] = redact_text(logentry["message"])
        if isinstance(logentry.get("formatted"), str):
            logentry["formatted"] = redact_text(logentry["formatted"])
        logentry.pop("params", None)

    exception = event.get("exception")
    if isinstance(exception, dict):
        for value in exception.get("values") or []:
            if not isinstance(value, dict):
                continue
            if isinstance(value.get("value"), str):
                value["value"] = redact_text(value["value"])
            stacktrace = value.get("stacktrace")
            if isinstance(stacktrace, dict):
                for frame in stacktrace.get("frames") or []:
                    if isinstance(frame, dict):
                        frame.pop("vars", None)

    correlation = current_correlation_id()
    if correlation:
        tags = event.setdefault("tags", {})
        if isinstance(tags, dict):
            tags["correlation_id"] = correlation

    return event


def init_error_tracking(settings: Settings | None = None) -> bool:
    """Initialise Sentry if a DSN and the SDK are both present.

    Returns whether error tracking is live, and **degrades quietly when it
    is not**: no DSN is the normal state in development and in the test
    suite, and an API that refuses to boot without an error tracker has
    turned observability into an availability risk.

    Fails *closed*, though, in the one case that matters. Every PHI-related
    kwarg below is passed in a single `init` call; if the installed SDK
    rejects any of them (a renamed option, an older major version), Sentry
    is **not** initialised at all. A tracker running with
    `include_local_variables` back at its default would ship transcripts to
    a third party, so "start without the safety options" is not one of the
    available outcomes.
    """
    global _sentry_initialised
    settings = settings or get_settings()

    if _sentry_initialised:
        return True
    if not settings.sentry_dsn:
        log_event(logger, "observability.error_tracking.disabled", reason="no_dsn")
        return False

    try:
        # No stub package, and deliberately not a hard dependency — see this
        # function's docstring on degrading without it.
        import sentry_sdk  # type: ignore[import-not-found]
    except ImportError:
        # Reported at WARNING rather than raising: a deploy that wants
        # Sentry and does not have it should be visible, not fatal.
        log_event(logger, "observability.error_tracking.disabled", level=logging.WARNING, reason="sdk_missing")
        return False

    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            release=settings.sentry_release,
            # PHI, in the four places Sentry looks for it by default.
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            max_breadcrumbs=0,
            # Tracing spans carry route templates and SQL descriptions.
            # Off by default (0.0) rather than sampled: a pilot with one
            # clinic has no performance question that needs distributed
            # tracing, and every enabled channel is a channel to audit.
            traces_sample_rate=settings.sentry_traces_sample_rate,
            before_send=_before_send,
            # Not "scrub each breadcrumb" — no breadcrumbs at all. See
            # `_before_send`.
            before_breadcrumb=lambda crumb, hint: None,
        )
    except TypeError:
        log_event(
            logger,
            "observability.error_tracking.disabled",
            level=logging.ERROR,
            reason="unsupported_sdk_options",
        )
        return False

    _sentry_initialised = True
    log_event(logger, "observability.error_tracking.enabled", origin=settings.environment)
    return True


def capture_exception(exc: BaseException) -> None:
    """Hand an exception to Sentry if it is live, and never fail because it
    is not. Wrapped so nothing else in the codebase has to import
    `sentry_sdk` or know whether it exists.
    """
    if not _sentry_initialised:
        return
    try:
        import sentry_sdk  # type: ignore[import-not-found]

        sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 - telemetry must never break the request it describes
        logger.debug("Could not report an exception to the error tracker", exc_info=True)


# ---------------------------------------------------------------------------
# HTTP middleware
# ---------------------------------------------------------------------------

#: Paths whose *successful* requests are not logged. A readiness probe
#: every few seconds would otherwise be ~95% of the log by volume, which
#: is the same "unreadable trail" problem decision 0032 solved for the
#: audit log by coalescing. Failures on these paths are always logged —
#: a health check that started failing is the single most interesting line
#: the API can produce.
_QUIET_PATHS = frozenset({"/health", "/ready", "/live", "/healthz", "/readyz"})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1").strip()
    return None


class CorrelationIdMiddleware:
    """Binds a correlation ID to every HTTP request, echoes it back, and
    emits one structured access line per request.

    A raw ASGI middleware for the same reason `SecurityHeadersMiddleware`
    is (see `app/main.py`): it runs on every request including streamed
    ones, and `BaseHTTPMiddleware` would add an anyio task pair per request
    to do work that is a dict lookup and a timer.

    Added **outermost** in `main.py`, which is what makes the access log
    complete: `CORSMiddleware` short-circuits a rejected preflight and an
    unhandled exception propagates past the router, and a request logger
    mounted inside either of those simply does not see those responses —
    the exact gap that makes a CORS misconfiguration invisible in the log
    (Phase 2.1 hit this: "the preflight is rejected and the request never
    reaches a route, so nothing appears in the API log at all").
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.logger = logging.getLogger("remedy.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = sanitize_correlation_id(_header(scope, CORRELATION_HEADER.encode()))
        # The path only — never the query string. A query string is the one
        # part of a URL a client controls freely, and `?q=Maria+Santos` is a
        # patient name in a log file. The same reasoning that keeps URLs out
        # of the Referer header (`referrer-policy: no-referrer` in main.py).
        path: str = scope.get("path", "")
        method: str = scope.get("method", "")

        with correlation_scope(inbound, origin="req") as correlation_id:
            started = time.perf_counter()
            status = 0

            async def send_wrapper(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                    MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            except BaseException as exc:
                # The response has already been abandoned; this is the only
                # record that the request existed at all.
                log_event(
                    self.logger,
                    "http.request.unhandled",
                    level=logging.ERROR,
                    exc_info=True,
                    http_method=method,
                    http_path=path,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    error_type=type(exc).__name__,
                )
                raise
            else:
                if path in _QUIET_PATHS and 200 <= status < 400:
                    return
                log_event(
                    self.logger,
                    "http.request",
                    level=logging.WARNING if status >= 500 else logging.INFO,
                    http_method=method,
                    http_path=path,
                    http_status=status,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )


__all__ = [
    "CORRELATION_HEADER",
    "CorrelationIdMiddleware",
    "LOGGABLE_FIELDS",
    "PHISafeJSONFormatter",
    "PHISafeTextFormatter",
    "PHIScrubbingFilter",
    "capture_exception",
    "configure_logging",
    "correlation_scope",
    "current_correlation_id",
    "init_error_tracking",
    "log_event",
    "new_correlation_id",
    "redact_text",
    "register_sensitive",
    "safe_exception_summary",
    "sanitize_correlation_id",
    "scrub_record",
    "scrub_value",
    "sensitive_scope",
    "stage_timer",
]
