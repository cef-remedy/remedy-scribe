# 0010 — `Note.status`'s "enum" had no DB-level constraint until now, and a second bug hid behind it

**Phase:** 0.4 · **Decided by:** implementation (empirically verified, not assumed) · **Date:** 2026-08-25

**Decision:** every `Enum(SomePythonEnum, native_enum=False)` column in this
codebase now explicitly passes **both** `create_constraint=True` and
`values_callable=lambda enum_cls: [m.value for m in enum_cls]`.

**What was actually wrong, found by testing rather than reading:**
1. `models/note.py`'s docstring claimed "the Enum column type below
   constrains the DB to these four values." Compiling the table's DDL and
   reading it showed no `CHECK` constraint at all. SQLAlchemy 2.0 changed
   `Enum.create_constraint`'s default to `False`; without the explicit flag,
   a non-native enum column is a plain `VARCHAR` with purely app-side
   (Python) validation — exactly the "our code only ever writes valid
   values" claim the checklist explicitly calls weaker than a real DB
   guarantee.
2. Turning `create_constraint=True` on (for `Note.status` and the two new
   enum columns) revealed a **second, worse bug**: SQLAlchemy renders the
   generated `CHECK` constraint using each enum member's **name**
   (`'RECORDING'`) by default, not its **value** (`'recording'`) — while
   the column itself stores `.value` strings. Without `values_callable`,
   the constraint would have rejected every legitimate write the
   application makes. A test that inserted a known-good value
   (`test_db_accepts_every_valid_pipeline_status`) caught this before it
   ever reached a migration.

**Options considered:** (a) fix both flags on every enum column, as done;
(b) fix only `create_constraint` and leave `values_callable` alone, trusting
manual review to catch value/name mismatches case-by-case; (c) switch to
native Postgres `ENUM` types instead of `native_enum=False` + CHECK.

**Why:** (b) is exactly the failure mode that produced bug #2 in the first
place — trusting that a generated constraint matches stored data, without
verifying it. (c) is a bigger, unrelated migration (loses SQLite portability
for tests, and native Postgres enums are notoriously painful to ALTER later)
and isn't what this phase asked for.

**What would change my mind:** if a future enum column is added without
copying this pattern and a similar mismatch slips through, that's a sign
this needs a lint rule or a shared helper (`app/db/enums.py:status_column(...)`)
rather than relying on every author remembering both flags by hand.
