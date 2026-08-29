# Runbook — observability, alerts and cost

**Phase 5.2 (P0-8) · Last exercised: 2026-08-29 · Decision:
[0037](../decisions/0037-logs-are-assembled-from-an-allow-list-not-scrubbed-after.md)**

What this system tells you about itself, how to read it, and — stated
first, because it is the part that is easy to assume — **what still does
not exist.**

---

## Read this before you point anything at production

The numbers below are safe to ship anywhere. The **logs** are safe to ship
anywhere *because* of `app/core/observability.py`, and that safety has one
prerequisite: nothing may add a second log handler with a different
formatter, and nothing may re-enable Sentry's default capture options.
Both are enforced in code (decision 0037), and both are one careless
config change from being untrue.

Two rules for anyone touching logging here:

1. **Never log an f-string.** `logger.info("stage done for %s", id)`, not
   `logger.info(f"stage done for {id}")`. The scrubber rebuilds a message
   from its *template* and its args; an f-string arrives pre-interpolated
   with nothing left to scrub. A test fails the build if you do
   (`test_no_source_file_logs_an_f_string`).
2. **Prefer `log_event`.** `log_event(logger, "pipeline.stage.finished",
   encounter_id=..., duration_ms=...)` emits an event name plus
   allow-listed fields and has no free-text channel at all.

---

## What exists, and what does not

| | status |
|---|---|
| Structured JSON logs, PHI-scrubbed at the record factory | **live** |
| Correlation ID: browser → API → Celery chain → note | **live** |
| Per-stage latency, failure counts, queue depth, upload stalls | **emitted** as `metric` events every 5 min |
| Per-consult cost | **estimated** and stored in Redis; token counts are inferred, not measured |
| Alert *rules* evaluated every 5 minutes | **live** (`pipeline.monitor_pipeline_health`) |
| Alert *delivery* to a human | **only via Sentry**, and only if `SENTRY_DSN` is set |
| Sentry project / DSN | **not provisioned** — nobody has an account |
| Log shipping / retention / search | **not built** — logs go to stdout |
| Dashboard | a terminal report (`python -m app.core.metrics`), not a web page |

Read the last four rows literally. Today, with no DSN configured, a
critical alert is an `ERROR` line in a log file on the VM, and **nothing
reads that file.** Emitting a number is not monitoring it, and this
runbook does not pretend otherwise.

---

## Tracing one consultation end to end

Every HTTP response carries `x-correlation-id`, and the web client both
sends and remembers it (`apps/web/src/lib/telemetry.ts`). Ask the doctor
for it, or read it from the browser console's `[telemetry]` line.

```bash
# everything that ID touched — request, transcription, note generation
docker compose logs api worker beat | grep '"correlation_id": "req-1a2b3c4d"'
```

IDs say where they came from:

| prefix | origin |
|---|---|
| `web-…` | minted by the browser, adopted by the API |
| `req-…` | minted by the API (no inbound ID, or an unusable one) |
| `pipeline-…` | a Celery chain started with no ambient ID |
| `sweep-stuck-…` | one run of `sweep_stuck_encounters` |
| `sweep-retention-…` | one run of `sweep_expired_retention` |
| `monitor-…` | one run of `monitor_pipeline_health` |

**A sweep's ID covers the run, not the request.** An encounter re-kicked
by a sweep gets the *sweep run's* ID, not the ID of the upload that
originally created it. That is deliberate: it answers "what else did this
same sweep run touch?", which is the question that distinguishes a broker
problem (a burst sharing one `sweep-` ID) from a per-encounter problem.

An inbound `x-correlation-id` that is not `[A-Za-z0-9_.:-]{1,64}` is
discarded and replaced, never cleaned up — an attacker-controlled string
in a log line is how someone edits the record of their own visit.

---

## The dashboard

From `apps/api/`, on the VM:

```bash
.venv/Scripts/python.exe -m app.core.metrics     # Windows
.venv/bin/python -m app.core.metrics             # Linux
```

Prints pipeline failure rates, per-stage latency percentiles, stuck
encounters, queue depth, upload stalls, scheduled-job heartbeats, the cost
summary, and any firing alerts. It reads Postgres and Redis directly and
takes no arguments.

It is a terminal report on purpose: a chart server is one more thing to
secure on a box that holds PHI, and the audience is one operator who is
already SSH'd in. The same numbers are emitted as `metric` events, so when
there is a log pipeline the page can be built there without touching a
single call site.

**Percentiles come from Redis and Redis here is not durable.** A `FLUSHALL`
or a container rebuild loses cost and latency history. The bounded fix is
in the follow-ups.

---

## Cost, against the PRD's <$0.10/consult

Both legs go to Groq (decisions 0018 and 0035), and they are billed on
different axes — ASR per hour of **audio**, note generation per **token**.
At the rates in `Settings` the audio leg is ~20× the generation leg, so
**the budget is effectively a duration budget**:

| consultation | ASR | note gen | total |
|---|---|---|---|
| 10 min | $0.0185 | $0.0013 | **$0.020** |
| 30 min | $0.0555 | $0.0028 | **$0.058** |
| 45 min | $0.0833 | $0.0039 | **$0.087** |
| 60 min | $0.1110 | $0.0050 | **$0.116** |

**Break-even is ~52 minutes of audio.** Under it you are inside the PRD's
target; over it you are not, and no amount of prompt tuning will change
that because generation is 5% of the bill.

Every figure above is an **estimate**. Token counts are inferred from
character counts (`COST_CHARS_PER_TOKEN`, default 4) because neither
`services/asr/groq.py` nor `services/note_generation/groq.py` reads the
`usage` block the vendor already returns. The per-unit *prices* are
settings, not guesses — but a price sheet is not an invoice:

```bash
# quarterly: reconcile the model against a real bill
#   1. Groq console -> Usage -> the month's audio-seconds and token counts
#   2. compare with the report's consult count x the figures above
#   3. if they disagree, fix COST_* in the environment, not the code
```

If the estimate is wrong it is most likely wrong **optimistically**:
Taglish tokenises worse than English on a vocabulary trained mostly on
English, so the real chars-per-token is probably below 4, which means more
tokens and more cost than shown.

---

## Alerts: what fires, and what you do about it

All rules live in `app/core/metrics.py:evaluate_alerts`, are evaluated
every 5 minutes by `pipeline.monitor_pipeline_health`, and are emitted as
`alert.firing` events. **`critical` is emitted at `ERROR` specifically
because Sentry's logging integration turns an `ERROR` record into an
issue** — that mapping is the entire delivery mechanism.

Every rate rule has a minimum sample size (`ALERT_PIPELINE_MIN_SAMPLE`,
default 5). Without one, the first failure of a quiet morning is a 100%
failure rate and within a week everyone has learned to ignore the alert,
which is worse than having none.

| rule | severity | default | first thing to check |
|---|---|---|---|
| `pipeline_failure_rate` | critical | >10% per stage | `GET /encounters/failed`; then the worker log for one of their correlation IDs. A vendor outage and a bad API key look identical in the rate and completely different in the *latency* — fast failures mean config, slow ones mean the vendor. |
| `stuck_encounters` | critical | >3 | Stuck work is supposed to self-heal every 5 minutes. A standing population means the **rescue** is broken, not that work is queued. Check the beat process, then whether `run_pipeline` is raising. |
| `queue_depth` | warning | >20 | Are any workers alive? `celery -A app.tasks.celery_app inspect active`. A deep queue with live workers is a throughput problem (Groq rate limits — see decision 0035's TPM ceiling); with no workers it is a process problem. |
| `queue_depth_unreadable` | critical | — | Redis is unreachable. The broker being down means **nothing is processing at all**, and the app will not have said so anywhere else. |
| `upload_failure_rate` | warning | >10% | Encounters created but never confirmed after `ALERT_UPLOAD_STALL_MINUTES` (60). Expect some: P0-2 lets a device hold audio offline until it has connectivity. A jump usually means the clinic's link, MinIO, or a presign failure. |
| `scheduled_job_stalled` | critical | 20 min / 200 min | The beat process. See below — this is the one that hides. |
| `cost_per_consult_over_target` | warning | p95 > $0.10 | Almost always consultation length, not the model. Check the audio-seconds in the `cost.consult.estimated` events before touching anything. |

### `scheduled_job_stalled` deserves its own paragraph

Two jobs exist *because* nothing was watching: `sweep_stuck_encounters`
rescues work that never ran, and `sweep_expired_retention` is the only
thing deleting derived PHI on schedule (decision 0033). Until this phase,
nothing watched **them**. If the `beat` container dies, the app keeps
serving, notes keep generating, and the only symptoms are that stuck
encounters stop being rescued and expired PHI stops being deleted. Both
are silent. The second is a Data Privacy Act problem accruing one
consultation at a time.

Each sweep stamps a heartbeat in Redis **after** its work, never before —
a job that crashes every time would otherwise look perfectly healthy.

```bash
redis-cli --scan --pattern 'remedy:obs:heartbeat:*' | xargs -n1 redis-cli get
docker compose ps beat && docker compose logs --tail=100 beat
```

**The honest limitation:** the monitor runs in the same beat process as
the jobs it watches. If beat dies, the monitor dies with them and nothing
fires. What it *does* catch — the far more common case — is beat alive
with a sweep failing or wedged. Closing the gap properly needs an external
check: a process supervisor with a restart policy, or an uptime ping that
alerts on silence. That is the deployment runbook's, and it is not done.

---

## Turning on error tracking (nobody has yet)

1. **Add the dependency.** In `apps/api/requirements.txt`, pinned and
   `pip-audit`-clean like everything else there:

   ```
   sentry-sdk==2.35.0
   ```

   The code works without it — `init_error_tracking` returns `False` and
   the API boots normally, which is the state today and in the test suite.

2. **Create the project** and set, per environment:

   ```
   SENTRY_DSN=https://<key>@<org>.ingest.sentry.io/<project>
   SENTRY_RELEASE=<git sha>
   SENTRY_TRACES_SAMPLE_RATE=0.0
   ```

3. **Do not change the scrubbing defaults.** They are in code, not
   settings, deliberately: `include_local_variables=False`,
   `max_request_body_size="never"`, `max_breadcrumbs=0`,
   `send_default_pii=False`, plus a `before_send` that strips
   `request`, breadcrumbs, non-allow-listed `extra`, and every frame's
   `vars`. If a future SDK rejects any of those kwargs, Sentry **does not
   initialise at all** — that is intended. Fix the kwarg; do not remove it.

4. **Verify before real PHI reaches it.** Raise a test exception from a
   staging worker with a transcript in scope and read the resulting issue.
   You are looking for: no request body, no breadcrumbs, no local
   variables in any frame, and a `correlation_id` tag.

5. **On the web client**, set `VITE_SENTRY_DSN`. There is no npm
   dependency — `telemetry.ts` speaks the envelope protocol directly and
   sends error class, a scrubbed message, stack **frame lines only** (the
   first line of a stack is `Name: message`, and the message is the part
   that can carry text), the correlation ID and the route path. This path
   has never been exercised against a live DSN; if it is silently
   dropping events, that is the first thing to suspect.

---

## Settings quick reference

| variable | default | notes |
|---|---|---|
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `text` only for a developer terminal; both scrub identically |
| `LOG_MAX_FREE_TEXT_CHARS` | `200` | raising this widens the PHI hole |
| `SENTRY_DSN` | unset | unset = no error tracking, and the API says so at boot |
| `METRICS_WINDOW_HOURS` | `24` | a clinic day |
| `ALERT_PIPELINE_FAILURE_RATE` | `0.10` | |
| `ALERT_PIPELINE_MIN_SAMPLE` | `5` | |
| `ALERT_STUCK_ENCOUNTERS` | `3` | |
| `ALERT_QUEUE_DEPTH` | `20` | |
| `ALERT_UPLOAD_FAILURE_RATE` | `0.10` | |
| `ALERT_UPLOAD_STALL_MINUTES` | `60` | generous: P0-2 allows offline holding |
| `ALERT_STUCK_SWEEP_MAX_AGE_MINUTES` | `20` | ~3× the 5-minute sweep |
| `ALERT_RETENTION_SWEEP_MAX_AGE_MINUTES` | `200` | ~3× the hourly sweep |
| `COST_TARGET_USD_PER_CONSULT` | `0.10` | the PRD's number |
| `COST_ASR_USD_PER_AUDIO_HOUR` | `0.111` | whisper-large-v3 list price |
| `COST_NOTE_USD_PER_MILLION_INPUT_TOKENS` | `0.15` | `openai/gpt-oss-120b` |
| `COST_NOTE_USD_PER_MILLION_OUTPUT_TOKENS` | `0.75` | |
| `COST_CHARS_PER_TOKEN` | `4.0` | the weakest number in the model |

There is deliberately **no setting** that disables PHI scrubbing, lowers
Sentry's guards, or turns on request-body capture. A leak you can cause
with an environment variable is a leak that will eventually be caused by
an environment variable, at 2am, by whoever is debugging.

---

## What Phase 5.2 still owes

- **A Sentry project.** Until one exists, `critical` alerts reach nobody.
- **An external check on the beat process**, since the monitor cannot
  watch its own host.
- **Log shipping.** Logs are stdout; nothing collects, retains or searches
  them, so `grep` on the VM is the only investigation tool.
- **Real token counts.** Two small changes — read `usage` from the Groq
  responses, persist it on `Note` — turn the cost dashboard from an
  estimate into a measurement.
- **Durable cost history.** Redis is the store today; a `FLUSHALL` loses
  it.
