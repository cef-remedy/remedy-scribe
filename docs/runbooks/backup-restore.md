# Runbook — Postgres backup and restore

**Phase:** 4.3 (P0-8) · **Status:** procedure written **and executed**
**Last restore drill:** 2026-08-28 — real dump, real restore, real
verification, against the local dev Postgres. Result: pass. Details in §5.
**Related:** [0034](../decisions/0034-an-untested-control-is-a-hope.md),
[key-rotation](key-rotation.md), [breach-response](breach-response.md)

> The checklist's phrasing is the whole point of this document: *an
> untested backup is a hope.* Every command below was run on 2026-08-28
> and its real output is recorded in §5. Where something was **not** run,
> §6 says so by name.

## 1. What has to be backed up, and what a backup alone is worth

Three artifacts, and the relationship between them is the interesting part.

| Artifact | Contains | Backed up by |
|---|---|---|
| **Postgres** | All PHI columns (Fernet ciphertext), the append-only consent ledger, the audit log, all note text and revisions | This runbook |
| **Object storage** (MinIO / S3) | Consultation audio | Bucket replication / versioning — **[5.1]**, not covered here |
| **`PHI_ENCRYPTION_KEY`** | Nothing. It is what makes the first row readable | [key-rotation](key-rotation.md) |

**The two-artifact rule: the database dump and the PHI key must never
travel or rest together.** A dump on its own is a pile of ciphertext in
the columns that matter — losing a copy of it is bad, not catastrophic. A
dump *plus* the key in the same bucket, the same backup job, or the same
`tar` is a complete, decryptable copy of every patient record in the
system, in one file, with one thing to steal. Any backup automation that
"helpfully" includes the environment file has quietly converted an
encrypted-at-rest system into a plaintext one.

The corollary is the failure people actually hit: **a restore is not a
recovery until the key is present too.** Restoring the database without
the key produces a system that starts, serves, and returns
`InvalidToken` on the first patient. §4 tests for exactly that.

## 2. Taking a backup

Custom format (`--format=custom`), not plain SQL: it is compressed, it can
be restored selectively, and `pg_restore` can validate it without a
database. Run it against a replica where one exists.

```bash
# Local / compose. Replace the container name for other environments.
docker exec remedy-scribe-postgres-1 \
  pg_dump -U remedy -d remedy_scribe --format=custom --file=/tmp/remedy_scribe.dump
docker exec remedy-scribe-postgres-1 sha256sum /tmp/remedy_scribe.dump
docker cp remedy-scribe-postgres-1:/tmp/remedy_scribe.dump ./remedy_scribe-$(date -u +%Y%m%dT%H%M%SZ).dump
```

Then, before it leaves the host:

1. **Record the SHA-256** alongside the file. A silently truncated dump
   restores partially and reports success for the parts it got.
2. **Encrypt it.** The PHI columns are ciphertext, but the dump still
   carries plaintext birthdates, clinician emails, audit rows and note
   metadata — that is personal information under the DPA whether or not
   the name column is encrypted. `age -r <recipient>` or the object
   store's own SSE-KMS; not the same key as `PHI_ENCRYPTION_KEY`.
3. **Copy it off the host**, to storage that a compromise of the database
   host cannot reach or delete.

### Schedule and retention **[5.1]**

Nightly full dumps are the floor. **Managed Postgres with PITR is the
actual target** (checklist 5.1) — a nightly dump means the worst case is
losing a day of consultations, and a clinic day of un-recoverable notes is
not an acceptable RPO for a clinical record.

**Backup retention is an open compliance question, not an engineering
default.** Phase 4.4 deletes expired audio and transcripts from the live
system; backups are outside its reach, so PHI a patient asked to have
deleted, or that passed its retention window, continues to exist in every
backup until that backup expires. That means backup retention is an upper
bound on the retention promise the clinic can honestly make. **Owner:
DPO/Legal.** Until they set it, do not let backups accumulate
indefinitely by default.

## 3. Restoring

**Never restore over a live database.** Restore into a new database and
repoint the application, so the damaged original is still available for
investigation — which matters especially if the reason for the restore is
a suspected breach.

```bash
# 1. A fresh database, never the live one.
docker exec remedy-scribe-postgres-1 psql -U remedy -d postgres \
  -c 'CREATE DATABASE remedy_scribe_restore;'

# 2. --exit-on-error --single-transaction: a partial restore that reports
#    success is the failure mode this is guarding against. Either the whole
#    dump lands or nothing does.
docker exec remedy-scribe-postgres-1 \
  pg_restore -U remedy -d remedy_scribe_restore --exit-on-error --single-transaction \
  /tmp/remedy_scribe.dump

# 3. Verify (section 4) BEFORE repointing anything.
# 4. Repoint DATABASE_URL, restart the API and workers (get_settings() is
#    lru_cached; the new URL is only read at process start).
```

## 4. Verification — the part that makes it a test rather than a hope

Run all five. A restore that passes only the first is the one that looks
fine for a week.

**4.1 — Row counts match the source, table by table.**

```bash
Q="select 'alembic_version' t, count(*) from alembic_version
 union all select 'audit_logs', count(*) from audit_logs
 union all select 'clinicians', count(*) from clinicians
 union all select 'consent_ledger_entries', count(*) from consent_ledger_entries
 union all select 'encounters', count(*) from encounters
 union all select 'login_attempts', count(*) from login_attempts
 union all select 'note_revisions', count(*) from note_revisions
 union all select 'notes', count(*) from notes
 union all select 'patients', count(*) from patients
 union all select 'refresh_tokens', count(*) from refresh_tokens
 union all select 'transcripts', count(*) from transcripts order by 1;"
docker exec remedy-scribe-postgres-1 psql -U remedy -d remedy_scribe          -At -F'|' -c "$Q" > src.txt
docker exec remedy-scribe-postgres-1 psql -U remedy -d remedy_scribe_restore  -At -F'|' -c "$Q" > dst.txt
diff src.txt dst.txt && echo "row counts identical"
```

**4.2 — `alembic_version` matches the application's head.** A restore of
an older dump into a newer application is a migration mismatch that
surfaces as a column-not-found error under load, not at startup.

**4.3 — The append-only trigger is *enforcing*, not merely present.**
This is the check that distinguishes a real restore from a plausible one.
`pg_dump` carries triggers, but a `--data-only` restore, a restore into a
schema built by `Base.metadata.create_all()`, or a `--disable-triggers`
flag would each produce a consent ledger that looks correct and silently
accepts mutation. P0-1's guarantee is that consent history cannot be
rewritten; verify it, do not assume it:

```bash
docker exec remedy-scribe-postgres-1 psql -U remedy -d remedy_scribe_restore \
  -c "delete from consent_ledger_entries where id = (select id from consent_ledger_entries limit 1);"
# MUST fail with: consent_ledger_entries is append-only (P0-1) - DELETE is not permitted
docker exec remedy-scribe-postgres-1 psql -U remedy -d remedy_scribe_restore \
  -c "update consent_ledger_entries set script_language='xx' where id = (select id from consent_ledger_entries limit 1);"
# MUST fail with the same message for UPDATE
```

Run the equivalent pair against `audit_logs`, which Phase 4.2 puts under
its own append-only trigger (migration `a0b1c2d3e4f5`). The 2026-08-28
drill predates that migration reaching the dev database, so this check was
**not** exercised — see §6.

**4.4 — The enum CHECK constraints survived** (decision 0010):

```bash
docker exec remedy-scribe-postgres-1 psql -U remedy -d remedy_scribe_restore -At \
  -c "select conrelid::regclass, conname from pg_constraint where contype='c' and connamespace='public'::regnamespace order by 1,2;"
# Expect: consent_ledger_entries|consenteventtype, encounters|encounterpipelinestatus, notes|notestatus
```

**4.5 — PHI actually decrypts under the live key.** The check nobody
thinks to run, and the only one that distinguishes "the bytes came back"
from "the records came back". Restoring a database whose ciphertext was
written under a key you no longer hold is not a recovery; it is an
archive.

```python
# From apps/api, with the environment the restored DB will be served under.
import json
import psycopg
from cryptography.fernet import Fernet
from app.core.config import get_settings

f = Fernet(get_settings().phi_encryption_key.encode())
with psycopg.connect("postgresql://remedy:remedy@localhost:5433/remedy_scribe_restore") as c:
    n = sum(bool(f.decrypt(ct.encode())) for (ct,) in c.execute("select full_name from patients"))
    t = sum(bool(json.loads(f.decrypt(ct.encode()))) for (ct,) in c.execute("select segments from transcripts"))
print(f"{n} patient names, {t} transcripts decrypted")   # never print the plaintext
```

Never print the decrypted values. Counting is the assertion; the contents
are PHI and a terminal scrollback is not a place for them.

Finally, drop the verification database. It is a second full copy of every
patient record in the system, and its existence is now the largest
unaudited PHI surface on that host.

```bash
docker exec remedy-scribe-postgres-1 psql -U remedy -d postgres -c 'DROP DATABASE remedy_scribe_restore;'
```

## 5. Drill log

| Date | Environment | Result |
|---|---|---|
| 2026-08-28 | Local compose Postgres 16 (`remedy-scribe-postgres-1`), dev data | **Pass** |

What actually ran on 2026-08-28, and what came back:

- `pg_dump --format=custom` produced a 70,012-byte dump,
  `sha256 c930434438309c30488b635e6bd219c2ae38a5eace7370ad02b932891f2660b4`.
- `pg_restore --exit-on-error --single-transaction` into a fresh
  `remedy_scribe_restore_check` database exited 0.
- **4.1** row counts identical across all 11 tables — `alembic_version` 1,
  `audit_logs` 99, `clinicians` 2, `consent_ledger_entries` 27,
  `encounters` 20, `login_attempts` 19, `note_revisions` 4, `notes` 8,
  `patients` 12, `refresh_tokens` 54, `transcripts` 8.
- **4.3** the restored `consent_ledger_entries_no_mutation` trigger
  rejected both `DELETE` and `UPDATE` with the P0-1 message. Enforcing,
  not merely present.
- **4.4** all three CHECK constraints present.
- **4.5** 12 of 12 patient names and 8 of 8 transcript segment blobs
  decrypted and parsed under the live `PHI_ENCRYPTION_KEY`.
- The verification database was dropped and the dump deleted from the
  container afterwards.

## 6. What was *not* tested, stated plainly

The drill above is a genuine dump/restore/verify cycle, and it is also a
small one. None of the following has been exercised, and none of it should
be assumed to work:

- **No production or production-scale data.** 12 patients and 70 KB. A
  restore that takes four minutes here may take hours at clinic scale, and
  the recovery-time objective is therefore unknown, not "fast".
- **No off-host copy, and no encryption of the dump.** The file never left
  the container. Steps 2 and 3 of §2 are written but unexercised.
- **No PITR / WAL replay.** Point-in-time recovery is a property of the
  managed Postgres that Phase 5.1 has not chosen yet. The only tested RPO
  is "whenever the last dump ran".
- **No object-storage restore.** Audio lives in MinIO/S3 and its backup
  story is entirely **[5.1]**. A restored database will happily reference
  audio objects that no longer exist — Phase 3's `_audio_state` `HEAD`
  check is what stops that from becoming a dead play button, but it will
  report `expired` for recordings that were never actually expired.
- **No `audit_logs` append-only check** (§4.3): the Phase 4.2 trigger
  migration had not been applied to the drilled database.
- **No restore-into-a-different-major-version test**, and no test of a
  deliberately corrupted dump.
- **Nobody but the author has run this.** A runbook that only its writer
  can execute has not been tested for the case it exists for, which is
  someone else running it at 3 a.m. Schedule a drill with a second person
  before the pilot.

**Cadence:** quarterly, and after any schema migration that adds a trigger
or a constraint. Add a row to §5 each time — including the failures,
which are the entries with information in them.
