# 0020 — Establishing a real mypy baseline surfaced a genuine production bug

**Phase:** cross-cutting (found during the 2026-08-25 `/production-checklist` refresh) · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** added `apps/api/mypy.ini` (didn't exist before — this was the
first time `mypy` had ever actually been run against this codebase,
despite being a declared dev dependency and a Phase 5.3 checklist item)
and `types-python-jose`/`types-passlib` to `requirements-dev.txt`. Fixed
every finding rather than suppressing them.

**What running it once, for the first time, found:**
1. **A real crash, not a type nitpick.** `GroqWhisperProvider.transcribe`
   passed `data=[(k, v), ...]` (a list of tuples) to `httpx.post`
   alongside `files=`. httpx's multipart encoder requires `data` to be a
   mapping whenever `files` is also present, and raises `TypeError`
   immediately — this would have failed on the very first real API call.
   The existing unit test never caught it because it replaced
   `httpx.post` entirely with a mock, which proves the *call site* looks
   right without proving httpx *accepts* it. Fixed to the mapping form
   (`{"timestamp_granularities[]": ["segment", "word"], ...}`, which
   httpx expands into repeated multipart fields correctly — verified by
   building a real, un-mocked httpx request and reading the encoded body).
2. **A real LSP violation.** `ASRProvider.model_version` is a writable
   class attribute (`= "unknown"`); `GroqWhisperProvider` overrode it with
   a read-only `@property`. mypy correctly rejects overriding a writable
   attribute with a read-only one. Fixed by setting it as a plain
   instance attribute in `__init__` instead — behaviorally identical
   here, since `get_asr_provider()` already returns a fresh instance
   per call.
3. Several `str | None` narrowing gaps in `app/api/routes/uploads.py` and
   `app/api/routes/auth.py` — all cases where runtime logic already
   guaranteed non-`None` (an earlier `if x is None: raise` a few lines up)
   but mypy couldn't trace the guarantee through the specific code shape.
   Fixed with explicit `assert` statements at the point of use, which
   both satisfies the type checker and documents the invariant in the
   code rather than leaving it implicit.

**Why fix #1 matters more than it looks:** it's the same *shape* of gap
Phase 0.5 closed at the database layer (SQLite tests never exercising
Postgres-only guarantees) — a test suite that replaces a real dependency
with a mock proves the code *calls* the dependency correctly, never that
the dependency *accepts* what's sent. Every mock in this codebase is a
candidate for the same class of blind spot; this is the first time it
showed up outside the DB/S3 layers those earlier phases already scrutinized.

**What would change my mind:** nothing about fixing what was found — but
worth revisiting whether `boto3`/`celery`'s missing type stubs
(currently silenced via `mypy.ini`, not resolved) are worth a heavier fix
(`boto3-stubs`) once this codebase's S3 surface grows past the handful of
calls in `storage.py`.
