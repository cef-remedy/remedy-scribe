# 0001 — How `assert_consent_valid` reads "current" consent from an append-only ledger

**Phase:** 0.1 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** `assert_consent_valid` loads every ledger row for the encounter
ordered by `created_at` ascending and folds over it in order (`given` → true,
`declined`/`withdrawn` → false), rather than querying for "the latest `given`
row with no later `withdrawn` row."

**Options considered:** (a) fold over full history in order, as implemented;
(b) a single query for `MAX(created_at)` among `given`/`withdrawn` rows and
branch on which event type won; (c) add a denormalized `current_consent`
column on `Encounter`, updated whenever a ledger row is inserted.

**Why:** (b) needs a tiebreak when two rows share a timestamp (real risk —
`created_at` has no uniqueness guarantee, and the ledger is the one table
in this system explicitly designed to be read as history, not as latest-row
state), and it silently drops `declined` from the fold, which matters if a
`declined` row can follow a `given` one. (a) treats all three event types
uniformly and needs no tiebreak because it never picks "the latest row" — it
just replays the sequence, which is also the mental model the ledger's own
docstring already commits to. (c) is a real option later if this table gets
large enough that a per-encounter fold becomes a hot path, but it reintroduces
exactly the kind of mutable derived state the append-only design was built to
avoid, so it's not worth it at current scale.

**What would change my mind:** if `EXPLAIN ANALYZE` on real clinic-week data
shows this fold showing up in a slow-query log (an encounter's consent history
is normally 1–3 rows, so this seems unlikely before very high volume), or if
a real timestamp collision is ever observed, revisit — that's push toward
(c) with careful invalidation, not toward (b).
