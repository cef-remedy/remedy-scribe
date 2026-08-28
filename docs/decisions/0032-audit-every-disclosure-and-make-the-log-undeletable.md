# 0032 — Audit every disclosure, and make the log undeletable

**Phase:** 4.2 · **Decided by:** implementation · **Date:** 2026-08-28

## The rule the checklist implies but does not state

4.2 has no 🧠. It has a ⚠️ that is really a diagnosis:

> "access logs" means *reads*, and reads are the ones developers forget,
> because nothing visibly breaks when they're missing.

That is true, and it is also not the hard part. The hard part is that once
you accept it, "should this be logged?" becomes a judgment call you make
twenty-three times, and every one of those calls is an opportunity to
decide that *this* particular read is too boring to bother with. Twenty
sensible-looking omissions later, the trail has a hole in exactly the
shape of whatever the reviewer is looking for.

So the decision below is a rule, applied mechanically, and every exception
is written down:

> **Log every disclosure of, or capability over, PHI. Do not log requests.**

"Disclosure" covers a read of a patient, note, transcript or audio object —
including a `POST` that reads (`/patients/match`), a list that touches
every record (`/encounters/loose`), and a URL that grants access to bytes
(`/upload/parts/{n}`, `/audio-url`). "Do not log requests" is what keeps
this from collapsing into a request log: `GET /upload/parts` returns part
numbers, sizes and ETags from S3, discloses nothing and grants nothing, so
it is the one endpoint in the API that touches an encounter and writes no
audit row. It says so in a comment, and a test asserts it, so the
exception is a decision rather than an oversight.

Applied, that took the trail from **7 call sites to 23**. The sixteen that
were missing were not exotic: reading an encounter, listing the loose-session
tray, matching a patient, linking a consultation to a chart, editing a note
section, capturing consent, finishing an upload.

## 1. Reads are logged after the data exists, not before

A 404 discloses nothing. A 409 discloses nothing. A 403 never reaches the
route. So `audit.record` is called after the response has been assembled,
never at the top of the handler.

The cost is stated plainly: **a denied access is not logged**, because
`require_role` raises inside a dependency and the route body never runs.
"Who tried to read the audit log and was refused?" is a real question and
this design cannot answer it. Fixing it properly means an exception handler
in `app/main.py` (owned by another phase this cycle) rather than a
copy-pasted `try` in every route. It is written up as a follow-up rather
than half-solved here, and there is a test asserting the current behaviour
so the gap is visible instead of assumed.

## 2. The one place fidelity is traded away, and why it is opt-in

`GET /encounters/{id}` is polled by the upload queue every 15 seconds until
the pipeline confirms (`apps/web/src/lib/queue`). It is also a genuine PHI
read — it discloses the patient linkage, the pipeline state and the note
id. Both facts are true at once, and they pull in opposite directions: a
20-minute transcription writes about 80 identical rows, which does not make
the trail more complete, it makes it unreadable. The human accesses either
side of a polling session are what a reviewer came for, and they are what
gets buried.

Options considered:

- **Log every poll.** Honest, and it is what a naive reading of the ⚠️
  demands. It also means the largest table in the system is ~95% machine
  noise, and the report a compliance officer actually runs returns eighty
  pages of one device asking "are you done yet?"
- **Don't log the poll.** The exact rationalization the ⚠️ warns about
  ("it's just a status check"), and it makes a real disclosure invisible.
- **A separate endpoint for machine polling.** Cleanest in theory. In
  practice it splits one read into two routes that return the same PHI,
  and the second one is where auditing quietly stops being applied.
- **Chosen: coalesce identical (actor, action, entity) reads inside a
  60-second window**, opt-in per call site via `coalesce_seconds`.

What survives: the first access is *always* recorded, and a continuing one
is re-recorded every window, so "this clinician had this record open from
09:14 to 10:02" is intact. What is lost: the exact hit count. What is
explicitly *not* affected: a different actor, a different record or a
different action never coalesce, and no write path passes the parameter —
`coalesce_seconds` defaults to `None`, so a call site has to ask for it.
Two of twenty-three do.

It is a plain `SELECT` with no locking. Two concurrent polls racing there
write two rows instead of one, which is harmless; the failure mode worth
engineering against is a *missing* row, and this cannot cause one.

## 3. Append-only, with the one hole the consent ledger left open

The trigger is the consent ledger's pattern (`a1c2e3f4b5d6`) for the
ledger's reason: the app's own DB role owns the table it created, table
owners bypass `GRANT`/`REVOKE` in Postgres, and a trigger that `RAISE`s
applies to the owner too. It holds even if the API's database credentials
are fully compromised — which is exactly the scenario in which someone
would want to edit an access log.

Two deliberate differences.

**DELETE is permitted once `retention_expires_at` has passed.** The consent
ledger blocks every mutation forever; an audit log cannot, because it has a
retention period and something has to enforce it. The alternatives were a
superuser escape hatch for the purge job (which is also an escape hatch for
whoever steals the purge job's credentials) or a `TRUNCATE`-and-reload
dance. Encoding the policy in the trigger is strictly better: before the
expiry date nobody may delete the row — not the app, not the purge job, not
a stolen credential — and after it, an ordinary `DELETE` works with no
special privilege. **`UPDATE` stays refused unconditionally, which is what
makes this safe**: `retention_expires_at` cannot be back-dated to bring a
row into deletable range, so the only route to a deletable row is to wait
seven years. That property has its own test.

**`TRUNCATE` is blocked too**, by a statement-level trigger. Row-level
triggers do not fire on `TRUNCATE`, so `TRUNCATE audit_logs` would have
erased the entire access log while leaving the "append-only" claim
technically intact. This was found by asking what the consent ledger's
trigger does *not* cover, not by a test failing — **and the consent ledger
still has the gap.** Fixing P0-1's table is a one-line migration, but it
belongs to whoever owns that requirement, not to this phase quietly
widening its blast radius. It is filed as a follow-up.

What none of this defends against is DDL. `DROP TRIGGER` is an owner
privilege and cannot be trigger-protected; the control there is that
migrations are an explicit deploy step (checklist 5.1), not something the
running application can do.

**A hash chain was considered and rejected.** It is the textbook answer to
"tamper-evident", and it would detect the one attack the trigger cannot: a
superuser who drops the trigger, rewrites rows, and puts it back. But a
superuser can also recompute the chain, so a chain only helps if its head
is published somewhere the attacker does not control — an external
timestamping service, or an append-only object store. That external anchor
is the actual control; the chain without it is ceremony. It also requires a
serialized write path (each row hashing the previous), which turns the
hottest insert in the system into a lock. If off-box anchoring lands, the
chain becomes worth building; until then it would buy the appearance of a
guarantee rather than one.

## 4. Retention: seven years, and the column is the policy

`retention_expires_at` is `NOT NULL`, stamped by the **column default** at
insert rather than by `audit.record`, so a row written by a future
background job or a test cannot slip through with a `NULL` that the purge
job would read as "skip forever".

It is stamped **at write time**, not computed at read time from a setting.
That is the point: a later policy change does not retroactively shorten the
life of rows already written, which is what turns "we keep access logs for
seven years" into a promise instead of a number someone can turn down after
the fact.

Seven years (2555 days) versus `AUDIO_RETENTION_DAYS`'s 90. The ratio is
the decision, not the number: the trail's whole job is to answer "who
looked at this patient's record?" during an investigation, and that question
is almost always asked about records that have themselves already been
deleted. An audit log that expires with the PHI it describes cannot answer
it. Seven years outlives any plausible complaint or investigation window by
a wide margin while staying a bounded commitment — and it is a placeholder
with a rationale, **not a legal finding**. The PRD's retention question is
still owned by Legal/Compliance.

It is read through `getattr(get_settings(), "audit_log_retention_days",
DEFAULT)` rather than as a plain settings attribute, because
`app/core/config.py` was owned by a concurrent phase and could not take a
new field. The moment that field lands, this picks it up with no code
change. That is a scheduling artifact, and it is written down as one.

## 5. Never PHI, because it cannot be taken back

Nothing in `audit_logs` may hold a patient name, note text, a search query
or an S3 object key. Existing code already followed this (`patient.search`
does not record the query; `encounter.audio.playback_url` does not record
the key) and this phase makes it a rule with a test behind it: a whole
consultation is driven through the API with distinctive PHI in it, and
every audit row is grepped for any of it.

The rule got sharper this phase, not weaker. Before, a stray patient name
in an audit row was a leak you could delete. Now the table is append-only
for seven years: **PHI written here is written permanently**, and no later
fix can take it back out.

Entity *ids* are stored, and the difference is real: a UUID is a surrogate
key with no meaning outside this database, it identifies nobody once the
row it points at is deleted, and a trail without it answers nothing. Two
ids go into `diff` for the same reason — `encounter.link_patient` records
the previous and new `patient_id`, because "linked to the wrong chart, and
to what before?" is not reconstructable afterwards from a row that has been
overwritten.

## 6. "Reviewable" means someone can actually answer the question

0.2 shipped an unfiltered, unpaginated list deliberately early so the RBAC
boundary had something to test (decision 0005). A list is not a review
interface. The requirement is a compliance officer sitting down and
answering *"who looked at this patient's record, and when?"* — so that is
an endpoint, `GET /audit-logs/access-report`, not a query a reviewer has to
compose.

Design choices worth recording:

- **Grouped by (actor, action), not a raw row dump.** The answer is a
  handful of names, not five hundred rows. The rows are one drill-down
  away via the filtered list.
- **The actor's name and email are joined in live** from `clinicians`,
  never copied into `audit_logs`. A report naming only UUIDs is one nobody
  reviews. Reading them live means a renamed account reports its current
  identity, and a deleted one degrades to nulls rather than to a name
  frozen years ago — and it keeps staff identity out of the append-only
  table, where it too would be permanent.
- **`LEFT OUTER JOIN`**, because `actor_clinician_id` is nullable and an
  unattributed access is the *most* interesting line in a breach
  investigation. An inner join would silently drop exactly those.
- **The list response stays a bare JSON array.** A `{items, total}`
  envelope is the better shape and it would break the contract 0.2
  published (and a test that asserts it). Pagination metadata rides in
  `X-Total-Count` / `X-Limit` / `X-Offset` instead.
- **Ordered by `created_at DESC, id DESC`.** Decision 0027 already recorded
  that rows written in one request can share a `created_at` to the
  microsecond; an unstable sort makes paging silently skip and repeat rows.
- **Reading the audit log is itself audited**, and pulling a patient's
  access report is recorded *against that patient*, so it shows up the next
  time someone pulls it. A review interface that leaves no trace makes the
  audit trail the one surface nobody is accountable for reading — and
  access logs get read precisely when someone is under suspicion.
- **Only the *names* of the filters used are recorded**, not their values —
  except `entity_type`/`entity_id`, which are surrogate keys and are the
  part worth being accountable for. A filter value is user-typed text, it
  can contain a patient name, and this table keeps what it is given for
  seven years. Same rule `patients.search` already followed.

## What would change my mind

- **If the coalescing window ever hides a real access.** The property it
  rests on is that the polling client and the human are the same actor on
  the same record; if a future client polls on behalf of someone else, or a
  shared service account appears, the window stops being safe and
  `encounter.read` should split into a machine action and a human one
  instead.
- **If off-box anchoring becomes available** (an external timestamping
  service, or a WORM bucket), the hash chain stops being ceremony and
  becomes the thing that catches a superuser who drops the trigger. The
  chain is only worth its serialized write path once its head is published
  somewhere the attacker cannot reach.
- **If Legal sets a retention period shorter than the deletion window of
  any PHI it describes**, the ratio argument in §4 fails and the column
  default is wrong — but note that rows already written keep their stamped
  date, deliberately, so a shortened policy applies going forward only.
- **If audit volume becomes a real operational cost** — the trail now grows
  with every read, not every write, which is a different order of magnitude
  — the answer is partitioning by month plus the retention purge, not
  logging less. Dropping actions to save space is how the hole gets back
  in.
