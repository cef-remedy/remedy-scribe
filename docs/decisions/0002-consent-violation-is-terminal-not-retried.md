# 0002 — A consent violation at task time is terminal, not retried

**Phase:** 0.1 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** in `transcribe_encounter`, `ConsentNotValidError` gets its own
`except` clause that sets `pipeline_status = "blocked_no_consent"` and
returns — it does not fall into the generic `except Exception` clause that
calls `self.retry(...)`.

**Options considered:** (a) a dedicated except clause, terminal, as
implemented; (b) let it fall through to the generic retry handler like any
other exception, so it retries up to `max_retries=3` before dying; (c) retry
with backoff indefinitely, on the theory that consent might be re-given.

**Why:** (b) and (c) both treat "consent is missing" as if it might resolve
itself with time, the way a rate limit or a network blip does — but nothing
in this system currently re-triggers the pipeline when a new `given` row
lands, so retrying just burns the retry budget for no benefit and then dies
into whatever generic failure state Phase 1.5 eventually defines, indistinguishable
from a real transcription failure. Making it terminal and distinctly-named
means an encounter stuck here is *findable and explainable* — "no consent,"
not "failed, cause unknown" — which is exactly what Phase 1.5's dead-letter
handling and Phase 6's instrumentation will want to query on.

**What would change my mind:** once re-consent has a UI path (Phase 2.3) that
can log a fresh `given` row for an already-blocked encounter, it would be
worth adding a small re-trigger (a route or a periodic sweep that re-enqueues
`blocked_no_consent` encounters once a new `given` row appears) rather than
requiring the encounter to be re-created from scratch. Not needed yet because
that UI doesn't exist.
