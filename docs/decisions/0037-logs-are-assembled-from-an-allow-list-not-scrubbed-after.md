# 0037 — Logs are assembled from an allow-list, not scrubbed afterwards

**Phase:** 5.2 · **Decided by:** implementation · **Date:** 2026-08-29

## The problem the checklist half-names

5.2's ⚠️ says to scrub PHI from logs and error reports, and lists three
ways it leaks: an exception carrying a transcript, a request body captured
by the error tracker, a debug log of a patient name. All three are real.
But the sentence that matters is the one after: *"it happens by accident."*

This codebase has the receipts. Twice:

- `note_generation/haiku.py:_extract_tool_input` raised
  `RuntimeError(f"...: {response_json!r}")` — the **entire** model response
  interpolated into an exception message. `pipeline.py:_mark_stage_failure`
  wrote `str(exc)[:500]` into `Encounter.last_pipeline_error`, a plain
  unencrypted `String(500)`. Generated clinical prose, in the clear, kept
  until the row was deleted. Found by porting to Groq, not by any test
  (docs/progress/4.0).
- That column's own comment argued it was safe: *"every exception raised
  from these two tasks is an infrastructure/vendor error ... never
  something built from transcript or note content."* Written truthfully.
  False one phase later. Nothing noticed.

Neither was carelessness. Both were the same failure: **a rule that lives
in a comment is enforced by memory, and memory is not a control.** That is
decision 0032's finding about the audit trail, arriving in a second place:

> Log every disclosure of, or capability over, PHI. Do not log requests.

0032 made auditing mechanical by removing the judgment call. The same move
is available here, and the rule is:

> **A log line is built from an allow-list of fields. Free text is a
> failure mode, not a feature.**

## Why a denylist is the wrong shape, stated once

The obvious implementation is a list of patterns — names, transcript-ish
strings, `patient`, `assessment` — applied on the way out. It protects
exactly the leaks someone already imagined. Every leak in this system's
history was one nobody imagined, in code whose author *had* thought about
PHI. A denylist would have caught neither of the two above, because
neither contained a recognisable marker: one was a JSON blob, the other
was ordinary English clinical prose.

Worse, a denylist creates the belief that logging is safe, which is the
condition under which people log more.

## What was built

Three boundaries. A careless call site passes through them whether it
wants to or not.

### 1. `logging.setLogRecordFactory` — the one thing that cannot be bypassed

Filters attach to loggers or handlers, and a handler without the filter
skips it. Sentry's logging integration does not even go through handlers.
There is exactly **one** log-record factory per process, and
`Logger.makeRecord` always calls it — so that is where the scrubbing lives
(`configure_logging`).

The factory rewrites the record **from its template, not its message**:

- With `%`-style args — which is how every existing call in this codebase
  is written — `record.msg` is a literal from the source file and the
  variable part is `record.args`. Scrub the args, then interpolate. Every
  pre-existing log line keeps working unchanged, and the values in it are
  now safe.
- With no args the string is of unknown provenance (it may be an
  f-string), so the whole of it goes through `redact_text`.

It also pre-renders a scrubbed traceback into `record.exc_text`.
`logging.Formatter` prefers an existing `exc_text` and never re-formats,
so the raw traceback cannot reach a log file — while Sentry, which reads
`exc_info` directly, still gets a real stack trace.

### 2. The formatter assembles; it does not filter

`PHISafeJSONFormatter` emits only keys in `LOGGABLE_FIELDS`. Not "copy the
record and delete the bad keys" — *assemble the line from the allow-list*.
The difference is what happens to a field nobody anticipated:
`extra={"transcript": ...}` emits the field's **name** in
`dropped_fields` and nothing else. The default for anything new is
silence.

No entry in `LOGGABLE_FIELDS` can hold free text. They are opaque
identifiers (already in `audit_logs`, meaningless without the database),
measurements, and enum values. That is what makes `log_event` — the
sanctioned API, an event name plus fields — PHI-free by construction
rather than by review.

### 3. `before_send` — Sentry's defaults are the opposite of what a PHI system wants

Configured before a DSN exists, which is the whole of the ⚠️'s advice.
Four channels, each on by default or produced by an integration:

| channel | default | why it matters here |
|---|---|---|
| `include_local_variables` | **True** | a frame in `generate_note` holds the entire transcript |
| `max_request_body_size` | `"medium"` | `POST /patients/match` carries a name; `POST /notes/{id}/sections` carries clinical prose |
| breadcrumbs | on | SQLAlchemy statements and every INFO log line |
| `send_default_pii` | False | already right; stated explicitly so it stays that way |

Frame locals are the one worth naming twice. **No part of the failing code
has to be PHI-related for that leak to fire.** An unrelated
`AttributeError` deep in a call stack that happens to pass through note
generation ships the consultation to a third party, in a field nobody
looks at, from code that never mentions logging. It cannot be fixed by
being careful about exception messages, and it is off at `init`.

And it **fails closed**: every safety option is passed in one `init` call,
and if the installed SDK rejects any of them — `include_local_variables`
has already been renamed once, from `with_locals` — Sentry is not
initialised at all. A tracker running with frame locals back at their
default is worse than no tracker.

## The gap this does not close, and the control that does

A **short, unregistered** piece of free text — `logger.info(f"patient
{name}")` — is shape-indistinguishable from `"Connection refused"`. No
scrubber can separate them, and anyone claiming otherwise is selling a
denylist. Two things address it, and neither is a scrubber:

- **`sensitive_scope` / `register_sensitive`.** Recognition by *identity*
  rather than by shape: the process is holding the exact string, so it can
  be replaced. `app/tasks/pipeline.py` opens a scope around each task —
  outside the `try`, deliberately, so the `except` that writes
  `last_pipeline_error` is inside it — and registers the transcript
  segments the moment they exist and the generated sections the moment
  they exist. Scoped, not global, because a process-wide registry of PHI
  strings *is* a PHI store: it would keep every transcript this worker
  ever saw resident for the life of the process, which is a worse problem
  than the one it solves.
- **A test that bans the construct.** `test_no_source_file_logs_an_f_string`
  scans `app/**/*.py` and fails on any `logger.*(f"...")`. With `%` args
  the template is a source literal and only the values vary, which is the
  exact property the factory relies on. Ruff's own `G004` is not in this
  repo's selected rule set (ruff.toml explains why the selection is
  narrow), so the ban lives in the suite instead — where a rule that only
  lived in a style guide is precisely the kind Phase 4.0 showed nobody
  remembers.

**So: mechanical, with one honest exception.** Structured fields are
allow-listed and cannot leak. Prose over 200 characters is replaced by its
length and a digest — not truncated, because the first 200 characters of a
generated Assessment is still a generated Assessment. Registered values
are replaced wherever they appear. Short unregistered free text in a code
path outside a sensitive scope would still be emitted, and the only
defence there is that the construct which produces it fails the build.

## Two smaller decisions inside this one

**Over-length text is replaced, never truncated.** All three of this
system's large PHI artifacts are prose — transcript, note sections,
revision history — so "cannot be said in 200 characters" is the one
property that reliably separates them from a vendor error string. A digest
survives so two occurrences of the same suppressed error are still
recognisable as the same error during an incident.

**Shape decides for interpolated values.** A whitespace-free token is an
identifier — a UUID, an object key, a model name, an HTTP status — and is
what logs are *for*, so it passes verbatim. Anything containing whitespace
is prose and gets the full treatment. This is why the digit-redaction rule
is written to exclude a UUID's twelve-digit tail: mangling encounter IDs
would defeat the correlation the same phase is building.

## The correlation half, briefly

Same phase, different property, one paragraph because it is not
contentious: a `ContextVar` does not survive serialisation into Redis, so
the ID is written into both Celery task signatures explicitly. It is only
ever *read* from the ambient context, never relied on to cross a process.

The two Beat sweeps have no inbound request, and leaving them blank would
make the one part of the pipeline nobody triggered also the one part
nobody can trace. Each mints a **run ID** (`sweep-stuck-…`,
`sweep-retention-…`, `monitor-…`) covering everything that run touches,
and every encounter a sweep re-kicks inherits it. A re-kicked encounter's
trace deliberately does **not** adopt its original upload's request ID: a
run identifier and a request identifier are different things, and merging
them would let one sweep of 200 encounters claim 200 unrelated requests.

## What would change my mind

- **If a short-PHI leak is found in practice** — a name in a log line from
  a path with no sensitive scope — the f-string ban is not enough and the
  next step is registering PHI at the point it is *decrypted*, in
  `EncryptedString.process_result_value`. That covers every read of every
  PHI column with no call-site discipline at all, and it is the strongest
  version of this design. It was not done now because it puts a substring
  scan on the hot path of every PHI read and belongs behind a measurement.
- **If the 200-character rule starts eating useful vendor errors**, the
  right response is not to raise it but to make the vendor clients emit
  structured fields (status, code, request id) the way `groq.py`'s parse
  errors already do — an error that needs 300 characters of prose is an
  error that has not been given a name yet.
- **If the pilot ever ships more than one clinic**, log-based alerting
  stops being adequate and the metric events should go to a real time
  series. They are already emitted as events with stable names for exactly
  that reason; nothing at the call sites would change.
- **If someone provisions Sentry and wants breadcrumbs back**, the honest
  version is per-integration: keep the navigation breadcrumbs, keep
  `before_breadcrumb` dropping the SQL and logging ones. Blanket-off was
  chosen because auditing each breadcrumb type on every SDK upgrade is not
  a task anyone will keep performing.
- **If per-attempt pipeline history is ever persisted** (a table, not a
  status column), `collect_snapshot`'s inference of success and failure
  from terminal status becomes unnecessary — and its known
  under-reporting, where a human-retried encounter counts only as a
  success, goes away with it.
