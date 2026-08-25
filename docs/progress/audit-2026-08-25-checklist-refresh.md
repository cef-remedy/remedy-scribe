# Checklist refresh & gap audit — 2026-08-25

**Status:** done · **Trigger:** `/production-checklist refresh the checklist artifact and update also the ASR model`
**Related decisions:** [0020](../decisions/0020-mypy-baseline-and-real-httpx-bug.md)

## What this was

Not a new checklist phase — a ground-truth re-verification and gap audit
pass over everything Phases 0–1.3 built, per the `production-checklist`
skill's update mode. Re-ran the test suite, ran `ruff`/`mypy` fresh,
booted the app, checked the Alembic chain, and did the skill's nine-class
gap audit against the newest code (Phases 1.1–1.3 hadn't had a dedicated
audit pass the way 0.1–0.5 did as they landed).

## What changed

- **A real bug, fixed:** `GroqWhisperProvider.transcribe` would have
  crashed on its first live call — `httpx.post(data=[...])` as a list of
  tuples alongside `files=` raises `TypeError` inside httpx's own
  multipart encoder. Never caught because the existing test mocked
  `httpx.post` entirely. Fixed to the dict-with-list-value form httpx
  actually requires; added a regression test that builds a real,
  un-mocked httpx request to prove the encoding works.
- **A real LSP violation, fixed:** `ASRProvider.model_version` — a
  writable class attribute — was overridden by a read-only `@property`
  on `GroqWhisperProvider`. mypy's first-ever run against this codebase
  caught it immediately. Fixed by setting it as a plain instance
  attribute in `__init__`.
- **A mypy baseline established for the first time.** `apps/api/mypy.ini`
  didn't exist; `mypy` had never actually been run despite being a
  declared dev dependency and an explicit (unchecked) Phase 5.3 item.
  Fixed the two real bugs above, added explicit `assert`s to resolve
  several `str | None` narrowing gaps in `uploads.py`/`auth.py` (all
  cases where runtime logic already guaranteed non-`None`, now made
  explicit rather than implicit), and added `types-python-jose`/
  `types-passlib` + a `mypy.ini` silencing only `boto3`/`botocore`/
  `celery`'s missing stubs (untyped, not unsafe). Result: **clean**,
  56 source files.
- **Two pre-existing, unrelated lint findings fixed in passing:**
  unused imports in `app/models/patient.py` and
  `app/services/note_generation/haiku.py`.
- **`docs/implementation-checklist.md`** — rewrote the "Current state"
  section with this session's fresh numbers (91 tests, clean lint/type-
  check, live `/health` check); added a refresh-log block at the top;
  updated every stale ElevenLabs/Scribe reference (the vendor changed in
  1.3) in Phases 1.3 and 2.2; marked the P0-3 diarization
  reverse-traceability gap explicitly (see below); updated Phase 4.4's
  wording to name the new `Transcript.retention_expires_at` column
  alongside the encounter-level one it already tracked.

## The reverse-traceability finding (Step 2 of the skill)

`remedy-scribe-prd.md`'s P0-3 explicitly requires "speaker diarization
enabled." Grepping the codebase for this confirms it: diarization has no
code behind it anywhere in the system, and structurally can't, given the
current ASR vendor (Groq-hosted Whisper — decision 0018, Phase 1.3, the
user's explicit call, made with the trade-off already understood at the
time). This isn't a new problem — it was flagged the moment 1.3 landed —
but the checklist's reverse-traceability pass is what confirms it's not
just *understood* but *searchable*: a future reader grepping for `P0-3`
finds the gap, not just a comment describing intent.

## Tests

91 passing (90 before this session + 1 new regression test for the httpx
encoding bug). `ruff check` and `mypy` both clean for the whole `app/`
tree.

## Notable follow-ups

- Decision 0020 flags `boto3`/`celery`'s missing type stubs as silenced,
  not resolved — revisit with `boto3-stubs` if the S3 surface grows past
  `storage.py`'s current handful of calls.
- The P0-3 diarization gap is now explicit in the checklist's "Current
  state" section, not just in decision 0018 — carried forward until
  Phase 1.4 (or a deliberate follow-up) resolves it one way or another.
