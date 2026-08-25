# Note generation: Haiku only, Luna dropped — 2026-08-25

**Status:** done · **Trigger:** `/production-checklist update the checklist also to reflect that generation model will be Haiku instead of Luna from now on`
**Related decisions:** [0021](../decisions/0021-haiku-only-luna-dropped.md)

## The clarifying question this started with

"Haiku instead of Luna" had two different code shapes: swap which one is
primary and keep both, or drop Luna entirely. These have materially
different consequences — one preserves P0-4's configured-fallback risk
mitigation, one removes it — so this was asked rather than assumed. The
user's answer: **Haiku only, Luna dropped entirely.**

## What changed

- **Deleted** `app/services/note_generation/luna.py`. Moved its
  `SYSTEM_PROMPT` constant into `haiku.py` (nothing else shares it now).
- **`app/services/note_generation/__init__.py`** — `get_note_generator()`
  now returns `HaikuNoteGenerator()` unconditionally; the `if/else` branch
  it used to have is gone since there's only one real option to branch to.
- **`app/core/config.py`** — `note_generator_provider` narrowed from
  `Literal["luna", "haiku"]` to `Literal["haiku"]`; `openai_api_key`
  removed (Luna was its only consumer).
- **Docstrings updated, not just deleted-quietly:** `note_generation/base.py`'s
  `NoteGenerator` ABC and `GeneratedSection.provider`'s comment both
  directly quoted P0-4's "Luna primary, Haiku fallback" framing — both
  now say plainly that this is superseded, and point at decision 0021
  rather than leaving a stale quote as the only explanation.
- **Both `.env` and `.env.example`** updated: `NOTE_GENERATOR_PROVIDER=haiku`,
  `OPENAI_API_KEY` line removed.
- **Four unrelated test files** (`test_note_lifecycle.py`,
  `test_postgres_specific.py`, `test_rbac.py`, `test_schema_constraints.py`)
  had `note_generator_provider="luna"` as a placeholder string, unrelated
  to testing generator identity — updated to `"haiku"` for the same reason
  the checklist itself gets scrubbed of stale vendor names: leaving a dead
  reference around is confusing to the next reader even where it's harmless.
- **`docs/implementation-checklist.md`/`.html`** — Phase 1.4's first task
  marked obsolete with the reason inline (not deleted — the skill's update
  mode explicitly says never silently delete an item); "Current state"
  updated; a new refresh-log entry added.

## How the pieces connect

```
app/core/config.py: note_generator_provider: Literal["haiku"]
        │
        ▼
get_note_generator() ── now unconditional, no branch
        │
        ▼
HaikuNoteGenerator (app/services/note_generation/haiku.py)
        │  imports its own SYSTEM_PROMPT now (was Luna's, moved)
        ▼
tasks/pipeline.py:generate_note() ── unchanged call site; the swap-point
                                       design (P0-4's own stated intent)
                                       meant this needed zero edits here
```

The `NoteGenerator` interface did exactly what it was designed for: the
provider swap was a config-type change and a factory-function edit, not a
change to the Celery task that actually calls it.

## Tests

91 passing, unchanged in count from before this edit — Phase 1.4 isn't
implemented yet, so nothing exercised the deleted code path either way.
`ruff` and `mypy` both clean (55 source files, one fewer than before —
`luna.py` is gone).

## A second finding, found only because of this change

The local, uncommitted `apps/api/.env` still had `NOTE_GENERATOR_PROVIDER=luna`
and a stale `ELEVENLABS_API_KEY=` line left over from before Phase 1.3.
Both had been silently ignored by `SettingsConfigDict(extra="ignore")` —
the stale ASR key caused no error at all, ever. `NOTE_GENERATOR_PROVIDER=luna`
only surfaced because narrowing the `Literal` type to `["haiku"]` made
Pydantic validate it strictly for the first time. Same shape of finding as
this whole checklist keeps surfacing: a guarantee (here, "the local env
matches what the code expects") that nothing checks isn't a guarantee —
it just hadn't been tested yet.

## Notable follow-ups

- Decision 0021's "what would change my mind": if Phase 6's edit-burden
  metric shows Haiku underperforming, build a real second provider then,
  chosen for what's actually best at that time — not by resurrecting
  Luna, which was never validated to begin with ("committed to without a
  bake-off").
- `.env` drift under `extra="ignore"` is structurally invisible for any
  setting that isn't validated against a closed set. Worth remembering
  the next time a `Literal` narrows — that's often the only thing that
  will ever catch it.
