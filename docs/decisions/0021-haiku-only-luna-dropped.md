# 0021 — Note generation: Claude Haiku 4.5 only, Luna dropped entirely

**Phase:** planning update ahead of 1.4 · **Decided by:** user · **Date:** 2026-08-25

**Decision:** `get_note_generator()` returns `HaikuNoteGenerator` unconditionally.
`LunaNoteGenerator` and its file (`app/services/note_generation/luna.py`) are
deleted, not kept dormant. `NOTE_GENERATOR_PROVIDER`'s type narrowed to
`Literal["haiku"]`; `OPENAI_API_KEY` removed from settings and both `.env`
files.

**This supersedes a written requirement, not just an implementation
detail.** `remedy-scribe-prd.md`'s P0-4 explicitly says: "Single fused call
using GPT-5.6 Luna, committed to directly without a formal vendor bake-off
... Claude Haiku 4.5 remains available as a **configured fallback** if Luna
underperforms in practice." That fallback was the roadmap's own named risk
mitigation for skipping the bake-off. Dropping Luna doesn't just change
which model is primary — it removes the fallback mechanism itself. If Haiku
underperforms in alpha, there is now no config-flag escape hatch to a
second real provider; the next step would be building one from scratch,
not flipping a setting.

**Options considered (per the clarifying question this decision answered):**
(a) swap primary/fallback, keep both classes — Haiku becomes default,
Luna's code stays as the fallback with roles reversed; (b) Haiku only,
Luna's code deleted, as chosen.

**Why (b), given (a) preserves more of the original risk mitigation:** this
mirrors the ASR vendor decision (0018) precedent, decided the same way —
an unused alternative with no distinguishing capability left to justify its
presence is dead weight, not a safety net, and this codebase already
treats "defined but unused" as a real finding class (0.2's `require_role`,
this session's own `haiku.py` docstring pointing at a deleted `luna.py`
before this fix). The explicit choice was the user's to make, not mine —
asked directly rather than assumed, since (a) and (b) have materially
different code consequences (a live fallback path vs. none).

**A second, smaller finding this surfaced:** the local `apps/api/.env`
(not committed — machine-specific) still had `ELEVENLABS_API_KEY=` and
`NOTE_GENERATOR_PROVIDER=luna` from before Phases 1.3 and this decision,
completely unnoticed because `SettingsConfigDict(extra="ignore")` silently
drops unrecognized keys — the stale `ELEVENLABS_API_KEY` line caused no
error at all. Only `NOTE_GENERATOR_PROVIDER=luna` failing the new
`Literal["haiku"]` validation surfaced the drift, and only because Pydantic
validates that field strictly. `.env` drift under `extra="ignore"` is
invisible by design for any field pydantic doesn't validate against a
closed set — worth remembering next time a setting's valid values narrow.

**What would change my mind:** if internal alpha's edit-burden metric
(Phase 6) shows Haiku underperforming badly enough to need a second
provider, rebuild the fallback then with whatever model is actually the
right second choice at that time — not by resurrecting Luna specifically,
since "committed to without a bake-off" was already a weak reason to trust
it over any other untested alternative.
