# 0034 — An untested control is a hope

**Phase:** 4.3 · **Decided by:** implementation · **Date:** 2026-08-28

## The problem the checklist does not name

4.3 reads as five unrelated chores: secrets, dependency scanning, a
password-hashing pin, backups, a breach runbook. They have one thing in
common, and the checklist says it out loud in exactly one of the five —
*"An untested backup is a hope."*

It is true of all five.

> A control you have not executed is a **belief about** your system, not a
> **property of** it.

The `bcrypt==4.0.1` pin carries a comment asserting why it exists; nobody
had checked whether the assertion was still true. "ruff and mypy clean,
206 passing" was a claim each progress doc made about one laptop, on one
day, unverifiable afterwards. Nothing had ever audited the dependency
tree. No backup had ever been restored. The breach runbook did not exist.

So the organising decision for this phase was: **execute every control
that can be executed here, and where one genuinely cannot be, say which
one and why rather than writing prose that reads like it was.** What
follows is what happened when each belief was actually tested.

## 1. The bcrypt pin says something that is no longer true

`requirements.txt` carried:

```
bcrypt==4.0.1  # passlib 1.7.4's self-test breaks against bcrypt>=4.1's stricter 72-byte check
```

That comment is a good comment — it names a reason, which is more than
most pins do. It is also **wrong about the boundary**, which was worth ten
minutes to find out. Built in throwaway environments, passlib 1.7.4
against each bcrypt release:

| bcrypt | Result |
|---|---|
| 4.0.1 | Works silently |
| 4.1.1 | Works, after logging a full traceback: `AttributeError: module 'bcrypt' has no attribute '__about__'` |
| 4.3.0 | Same |
| 5.0.0 | **Hard failure.** `ValueError: password cannot be longer than 72 bytes` raised out of `detect_wrap_bug` during `CryptContext(...)` construction — hashing and verification never run |

So the app was pinned three minor versions back from where the breakage
starts, on the strength of a log line. And the underlying situation is
worse than the comment implies: the failure is in passlib's **start-up
self-test**, which hashes an over-length secret to probe for a bug in
2011-era bcrypt wrappers. passlib 1.7.4 shipped in October 2020 and has
had no release since. It is not going to be fixed. Every future bcrypt
release is a coin flip.

**The decision: drop passlib, hash with argon2id, keep bcrypt as a
verify-only path.**

Three options were on the table:

- **Keep passlib, unpin bcrypt to <5.** Cheapest, and it re-buys the same
  problem with a shorter fuse. The comment would need updating to say the
  ceiling is now 4.x, and someone would hit it again.
- **bcrypt directly, no passlib.** Genuinely tempting: it removes the
  unmaintained wrapper, needs no migration at all, and every stored hash
  keeps verifying unchanged. It also keeps bcrypt's 72-byte truncation as
  a live property of the system.
- **Chosen: argon2id via `argon2-cffi`, with bcrypt retained for
  verification only.**

Argon2id is memory-hard where bcrypt is not, and it has no truncation
behaviour. The truncation is not academic here — measured against the
implementation:

```
legacy bcrypt, 100-char password : verifies correctly
                                   wrong password (same first 72 bytes) : ALSO verifies
new argon2id, 100-char password  : verifies correctly
                                   wrong password (same first 72 bytes) : rejected
```

For a clinical system where a doctor may well use a password manager's
long generated string, silently ignoring everything past character 72 is
a real weakening that no one would ever notice.

### Why bcrypt stays, and why the upgrade is not automatic

Existing `Clinician.hashed_password` values are bcrypt. `verify_password`
therefore branches on the stored prefix: `$argon2` → argon2, `$2a$/$2b$/
$2y$` → bcrypt, anything else → `False`. Verified end to end: hashes
produced by the *old* stack (passlib 1.7.4 + bcrypt 4.0.1) verify under
the *new* code, and wrong passwords are still rejected.

Two smaller calls inside that:

**An unrecognised hash returns `False` rather than raising.** passlib
raised `UnknownHashError`, which becomes a 500 — and a 500 on one account
and a 401 on every other is an oracle telling an attacker that account is
stored differently. It is also not hypothetical: the test suite seeds
`hashed_password="x"` in a dozen places.

**`password_needs_rehash` is a separate function that the caller must
choose to use.** The tempting design is to have `verify_password` upgrade
the hash in place. It cannot: the plaintext exists only for the instant of
a successful login, and `app/core/security.py` has no database session —
only `app/api/routes/auth.py` does. Hiding a write inside a function named
`verify` would also make a read-shaped call silently mutate a row. So the
capability is exposed and the wiring is left to the route, which means
**until that wiring lands, old bcrypt credentials stay bcrypt forever.**
That is not worse than today, and it is stated rather than implied.

The one real cost of argon2id over bcrypt is operational: memory per
verification (64 MiB at the library defaults) is a resource an API server
has to have. At clinic-scale login volume this is not a concern; at a
login-storm it is, and it interacts with the Phase 0.3 rate limiter that
already bounds attempts per IP.

## 2. "Not reachable" is a triage result, not a version policy

`pip-audit` had never been run. It reports **30 advisories across 6
packages** on the pinned tree. `npm audit` on `apps/web` reports **0** —
the checklist's note that "the mobile scaffold already reports
vulnerabilities" is stale, since decision 0024 retired the mobile client
and the browser client's tree is clean.

Every one of the 30 was checked for reachability in *this* application,
and — this is the uncomfortable part — **essentially none of them were
exploitable here:**

- **python-multipart** (7 advisories, all form-parsing DoS): no route in
  the app declares a `Form`, `File` or `UploadFile` parameter, so
  Starlette never invokes the parser. Uploads go direct to object storage
  by presigned URL (decision 0013), which is why.
- **starlette** `HTTPEndpoint` method-confusion: the app uses FastAPI
  routers, not class-based endpoints. `StaticFiles` UNC SSRF: nothing is
  mounted. Host-header URL reconstruction: nothing reads `request.url`.
- **cryptography** X.509 name-constraint bypasses and the ECDSA
  small-subgroup gap: the app uses Fernet and HMAC. It validates no
  certificate chains and parses no EC public keys.
- **python-jose** algorithm confusion: `decode_access_token` passes an
  explicit `algorithms=` allow-list, which is the documented mitigation.
  The JWE decompression bomb: nothing calls `jwe`.

It would have been easy, and defensible-sounding, to write "audited, no
action required" and move on. That is the wrong conclusion, for a reason
worth writing down: **every one of those findings is unreachable because
of a property of today's code, and none of those properties is enforced.**
One `Form(...)` parameter makes seven python-multipart advisories live. A
`StaticFiles` mount makes the SSRF live. The audit's value is not the
current triage; it is that the tree stops drifting.

So everything with a fix was bumped, and the tree went **30 findings → 1**,
verified by re-running `pip-audit` against the new pins. Then the full
suite, ruff and mypy were run against the bumped set in an isolated
environment before anything was written back.

The one survivor is `ecdsa` (CVE-2024-23342, a Minerva timing attack on
P-256). It has **no fix and will not get one** — the project considers
side channels out of scope. It arrives as a hard dependency of
python-jose, and this app signs HS256 only, so no ECDSA code path is
entered.

**It is therefore in an explicit `--ignore-vuln` list in the CI job, with
its reasoning in a comment, rather than the job being soft-failed.** That
distinction is the decision. `continue-on-error` on an audit job produces
a check everyone learns to scroll past; an ignore list produces a short,
named, re-examinable set of accepted risks. The real remedy — replacing
python-jose with PyJWT, which removes `ecdsa` entirely — needs
`app/core/security.py` and is recorded as a follow-up rather than smuggled
into a phase that was not about JWTs.

**CI runs the audits on a weekly schedule as well as on change**, because
the failure mode being defended against is an advisory landing against
code that did not change. An audit that only fires on push is silent
exactly on the weeks that matter.

## 3. The secret manager decision is to not make it here

There is no deployment. Choosing a secret manager now would be choosing
before the hosting target and before Legal has ruled on data residency for
Philippine health data — and the choice is downstream of both.

What *is* decidable now, and is the actual deliverable, is that **the
application is already indifferent to which manager wins.**
`pydantic-settings` resolves the process environment ahead of `.env`, so
any injector works with zero code change. That turns the checklist bullet
from a refactor into a deployment task, and the runbook's job is to name
precisely which deployment task:

- delete `env_file: ../apps/api/.env` from the production compose/unit
  file, and never copy a `.env` to a host;
- inject at `exec` time so plaintext exists only in process memory;
- hold the `PHI_ENCRYPTION_KEY` somewhere that survives the application
  being down, because it is the only secret whose loss is as bad as its
  leak.

The recommendation for the pilot is SOPS + age rather than Vault, on the
checklist's own principle of picking the least infrastructure that meets
the compliance bar: Vault is a highly available system whose
unavailability takes the app down with it, which is a poor trade for one
clinic. What SOPS does **not** give — a read audit trail, automatic
rotation, dynamic credentials — is written down as the trigger for
graduating, so the trade is revisited on evidence rather than forgotten.

## 4. A restore that only counts rows has not verified a restore

The backup/restore cycle was **executed**, not documented: `pg_dump
--format=custom` off the running Postgres, `pg_restore --exit-on-error
--single-transaction` into a fresh database, verify, drop. It passed.

The decision worth recording is what "verify" had to mean. Row counts are
the obvious check and they are close to worthless on their own, because
the two things this schema depends on are not rows:

**The append-only trigger has to be tested by trying to break it.** P0-1's
guarantee is that consent history cannot be rewritten. `pg_dump` carries
triggers, but a `--data-only` restore, a schema rebuilt by
`Base.metadata.create_all()`, or a `--disable-triggers` flag would each
produce a consent ledger that *looks* right and silently accepts mutation.
So the drill issues a real `DELETE` and a real `UPDATE` against the
restored ledger and requires both to be refused. They were, with the P0-1
message.

**PHI has to be decrypted, not merely present.** Every PHI column is
Fernet ciphertext. A restore whose ciphertext was written under a key you
no longer hold is not a recovery; it is an archive with a nice schema.
The drill decrypts every `patients.full_name` and every
`transcripts.segments` blob under the live key — 12 and 8 respectively —
and asserts the count, never printing a value.

That check is also what forces the **two-artifact rule** into the runbook:
the dump and the `PHI_ENCRYPTION_KEY` must never travel or rest together.
A dump alone is ciphertext in the columns that matter. A dump plus the key
in the same bucket is every patient record in the clinic, in one file,
with one thing to steal. Backup tooling that "helpfully" includes the
environment file converts an encrypted-at-rest system into a plaintext one
without anybody deciding to.

**And it surfaces a contradiction that is not engineering's to resolve.**
Phase 4.4 deletes expired audio, transcripts and revisions from the live
system. Backups are outside its reach. So PHI a patient asked to have
deleted continues to exist in every backup until that backup expires,
which makes backup retention an upper bound on the retention promise the
clinic can honestly make to a patient. That is a DPO/Legal decision, and
the runbook says so rather than picking a number that would read as policy.

## 5. The 72-hour clock makes detection the binding constraint

The breach runbook is grounded in this system's actual map: Fernet columns
in Postgres, audio in object storage, transcripts and audio sent to Groq
(decision 0018) — data that leaves the country — notes generated by an LLM
vendor, and queued audio sitting in IndexedDB on a clinician's laptop.

Two properties of that map drive the runbook's shape:

**Whether the PHI key is in scope is the first triage question**, because
it is the difference between a database incident and a plaintext
disclosure of every patient record. Nothing else changes the answer as
much, so nothing else goes first.

**The audit log is the only instrument that can scope a disclosure**, and
Phase 4.2 made it append-only, so it can be trusted afterwards. It holds
no PHI by design, which means it can be exported into an incident ticket
safely — a property that only matters on the day it matters.

The legal research produced one finding that changes what engineering
should do next. The NPC's 72 hours run from **knowledge of, or reasonable
belief in**, a notifiable breach — not from confirmation. So the
compliance-binding constraint is **detection**, not notification. And this
system has none: Phase 5.2 is unbuilt, there is no error tracking, no
alerting, no anomaly detection over the audit log. Today a breach is
noticed when a human happens to notice. Writing a beautiful notification
procedure on top of that would be the most misleading document in the
repository, so the runbook states it in §2 as its own largest gap.

Everything legal in that runbook is marked as needing counsel, and the two
`privacy.gov.ph` primary sources are cited as **not retrieved** (they
returned 403 to automated fetching) rather than quietly presented as read.
The roles table — DPO, incident lead, breach response team — is included
**empty**, because a runbook with an empty roles table is at least honest
about being a plan rather than a capability, and the DPA requires a
designated DPO before any of it can be executed.

## What would change my mind

- **On argon2id:** if login latency or memory under the Phase 6 pilot's
  real concurrency turns out to matter, argon2's parameters are tunable
  before the algorithm choice is — and if it still matters after tuning,
  bcrypt-direct at a raised work factor is the fallback, since it was
  already the second-best option here and needs no migration.
- **On the ignore list:** if the PyJWT migration lands, `ecdsa` leaves the
  tree and the `--ignore-vuln` entry should be deleted rather than
  inherited. If that list ever grows past about three entries, the audit
  job has stopped being a gate and the growth is the signal, not the
  entries.
- **On not choosing a secret manager:** if Phase 5.1 picks a managed
  platform whose native secret manager is free and already in the trust
  boundary, SOPS is strictly more machinery than the situation needs and
  the recommendation should be dropped without ceremony.
- **On backups:** the drill was 12 patients and 70 KB. If a production-
  scale restore turns out to take hours, the recovery-time objective, not
  the procedure, becomes the thing to fix — and that argues for managed
  Postgres with PITR (5.1) sooner rather than as a later upgrade.
- **On the breach runbook:** the first real incident will find something
  wrong with it. That is expected, and the document should be rewritten
  from what actually happened rather than defended.
