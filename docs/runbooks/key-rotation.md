# Runbook — rotating the PHI encryption key

**Phase 4.1 (P0-8) · Last rehearsed: 2026-08-28 · Decision:
[0031](../decisions/0031-phi-encryption-stays-in-the-application-layer.md)**

Every PHI column in this system — patient names, all four note sections,
both sides of every note revision, and the full transcript — is a Fernet
token written by `app/core/security.py`. `PHI_ENCRYPTION_KEY` is the key
that produces them.

**Lose that key and every one of those columns is unreadable forever.** No
backup of the database helps; the backup is ciphertext too. That is the
single fact this runbook exists to make survivable.

---

## Before anything else: is the key backed up?

Rotation is the *recovery* procedure for a leaked key. It is not a
substitute for the key existing somewhere a person cannot casually delete.
The checklist's ⚠️ asks for this before pilot, and it is a prerequisite for
everything below.

At minimum, for each environment holding real PHI:

- The key is in the environment's secret store, not only in a running
  process's environment.
- A sealed offline copy exists, held by someone who is not the person who
  can deploy the application.
- Its **fingerprint** is recorded where the on-call can see it, so "which
  key is production on?" is answerable without anyone handling the key:

  ```bash
  python -c "from app.core.config import secret_fingerprint; import os; print(secret_fingerprint(os.environ['PHI_ENCRYPTION_KEY']))"
  ```

  Fingerprints are truncated SHA-256 and are safe in a ticket or a chat
  message. Keys never are.

Secret storage itself is Phase 4.3's — see
`docs/runbooks/secrets-management.md`.

---

## When to rotate

| trigger | urgency |
|---|---|
| Key was, or may have been, exposed — logs, a screenshot, a shared `.env`, a departing staff member with deploy access | **Immediately.** Rotate, then treat as a breach (`docs/runbooks/breach-response.md`). |
| Scheduled cadence, if the DPO sets one | Planned window |
| Migrating to KMS envelope encryption (decision 0031) | Planned; same rewrite, different target |

Note what rotation does **not** do: it does not make previously exfiltrated
ciphertext safe. Anyone holding both a copy of the database and the old key
can still read what they took. Rotation limits future exposure and restores
control; it does not undo a disclosure.

---

## How it works, in one paragraph

Fernet tokens carry no key id, so there is no way to re-key without
rewriting every value. `MultiFernet` makes that an *online* operation:
`PHI_ENCRYPTION_KEY` is the key that encrypts, `PHI_ENCRYPTION_KEY_PREVIOUS`
is a comma-separated decrypt-only list, and the application reads a database
holding a mix of both. So the rewrite can run against a live system, and an
interrupted rewrite is a non-event — just run it again.

---

## Procedure

Times below are measured, not estimated — see
[Rehearsal](#rehearsal-2026-08-28). At six months of clinic data the whole
thing is roughly five minutes of machine time.

### 1. Generate the new key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put it in the environment's secret store immediately, alongside the current
one. **Do not delete anything yet.** Record both fingerprints on the change
ticket.

### 2. Take a backup, and confirm it restores

`docs/runbooks/backup-restore.md`. This is the step that makes every later
step reversible. A rotation that is interrupted is fine; a rotation run
against a database with no verified backup is not.

### 3. Put both keys in the running configuration

```
PHI_ENCRYPTION_KEY=<new key>
PHI_ENCRYPTION_KEY_PREVIOUS=<old key>
```

Restart the API and the Celery workers. **Both.** A worker still holding
only the old key will fail every transcript write the moment the API starts
producing new-key ciphertext.

From this point the application writes under the new key and reads under
either. Nothing is broken and nothing has been rewritten yet — which is why
this ordering is safe to do at any time of day.

Sanity-check that the running app can read existing PHI before continuing:
open one existing note in the UI. If that works, both keys are live.

### 4. Dry-run the rewrite

```bash
cd apps/api
python scripts/rotate_phi_key.py --new-key "$NEW_KEY" --old-key "$OLD_KEY" --dry-run
```

Reads and re-encrypts every value in memory and writes nothing. It proves
every value in the database is decryptable by the keys you hold *before* any
of them is modified, and it prints a realistic timing for step 5.

Any `FAILED` line here means a value no supplied key can read. **Stop.**
That is a pre-existing problem — a row from an older key, or corruption —
and rotating around it would leave it permanently unreadable. Find the key
it belongs to and add it with a second `--old-key`.

### 5. Run the rewrite

```bash
python scripts/rotate_phi_key.py --new-key "$NEW_KEY" --old-key "$OLD_KEY"
```

Safe to run while the clinic is working: it commits per batch, holds row
locks only for the batch in flight, and the application reads both keys
throughout. Safe to interrupt — re-running resumes correctly, because
re-rotating an already-rotated value is a no-op in effect.

Columns are **discovered from the schema by type**, not from a list in the
script, so a newly added encrypted column cannot be silently skipped. The
output names every column it touched; check the count against the 8 columns
across 4 tables below.

For a database heavy in transcripts, `--batch-size` trades memory for
round-trips. The default of 500 holds up to ~170 MB of transcript
ciphertext in memory per batch; lower it on a small instance.

### 6. Verify under the new key alone

```bash
python scripts/rotate_phi_key.py --new-key "$NEW_KEY" --verify-only
```

This is the gate on the irreversible step. It decrypts every value using
**only** the new key, so it fails on any row the rewrite missed. Exit 0 with
the full value count is the only acceptable result.

A run that completes without error in step 5 is *not* this evidence. Do not
substitute it.

### 7. Retire the old key

Only now:

1. Remove `PHI_ENCRYPTION_KEY_PREVIOUS` from the configuration and restart.
2. Re-run step 6 against the restarted app's configuration.
3. Open a note in the UI. If it renders, the old key is genuinely no longer
   load-bearing.
4. Destroy the old key **in the secret store**, after confirming no backup
   older than step 2 is still within its retention window. A restore of last
   week's backup needs last week's key — so the old key must outlive every
   backup that was taken under it. Note that on the change ticket, with the
   date it may actually be destroyed.

### 8. Record it

On the change ticket: both fingerprints, the row and value counts from step
5, the wall-clock duration, and the date the old key becomes destroyable.
The script prints fingerprints and never key material, so its output can be
pasted in as-is.

---

## Rehearsal, 2026-08-28

Run end to end against a real Postgres 16 (`postgres:16-alpine` in Docker,
schema built by `alembic upgrade head`, application and database on the same
Windows 11 host), seeded with **17,500 rows holding 35,000 encrypted values
and 1.73 GB of ciphertext** — approximately six months of a clinic running
40 consultations a day.

**Full rotation: 181.0 s (3 min 02 s).**

| table | rows | values | ciphertext | rotate | verify |
|---|---|---|---|---|---|
| `patients` (`full_name`) | 5,000 | 5,000 | 0.6 MB | 5.1 s | 0.12 s |
| `notes` (`assessment`, `plan`, `subjective`, `objective`) | 5,000 | 20,000 | 13.5 MB | 7.5 s | 0.47 s |
| `note_revisions` (`previous_text`, `new_text`) | 2,500 | 5,000 | 2.3 MB | 3.6 s | 0.23 s |
| **`transcripts` (`segments`)** | **5,000** | **5,000** | **1,714 MB** | **164.8 s** | **15.7 s** |
| **total** | **17,500** | **35,000** | **1,731 MB** | **181.0 s** | **16.5 s** |

Then verified: **35,000 of 35,000 values readable under the new key alone**,
and — the assertion that actually proves the rewrite happened — **0 of
35,000 readable under the old key.**

### What the numbers say

**Cost is bytes, not rows.** Transcripts are 14% of the values and 91% of
the time. A 15-minute word-level transcript is ~340 KB of ciphertext; the
other three tables together are 16 MB against the transcript table's 1.71 GB.

**Crypto is not the bottleneck.** Measured by running the same scan three
ways over the same 1.73 GB:

| what ran | how | time |
|---|---|---|
| read + decrypt every value | `--verify-only` | 16.5 s |
| read + decrypt + re-encrypt, no writes | `--dry-run` | 26.7 s |
| read + decrypt + re-encrypt + `UPDATE` | full run | **181.0 s** |

Encryption and decryption together account for **27 s of 181 s (15%)**. The
other 85% is Postgres rewriting large TOAST-ed text, one row per statement.
The same shape of finding as decision 0029, where ORM hydration rather than
decryption turned out to dominate patient search: on this codebase, the
crypto keeps not being the expensive part.

The practical consequence is that a faster cipher or a KMS would not make
rotation meaningfully quicker — only writing fewer or smaller rows would.

**So rotation cost scales with the retention window, not the practice.**
It grows with recorded minutes retained, not with the patient directory.
Phase 4.4's retention deletion is therefore also a rotation-cost control:
an expired transcript is a row that never has to be rewritten again.

**Extrapolating** (labelled as such — this part is arithmetic, not
measurement): the rewrite ran at ~10 MB/s of transcript ciphertext, and the
work is linear, so a year of the same clinic is roughly six minutes and
three years roughly eighteen. Rotation stops being comfortable somewhere
around 18 GB of retained transcript, which is the point decision 0031 names
as the trigger to reconsider KMS envelope encryption.

---

## The 8 encrypted columns

Reproduce this list from the schema rather than trusting it — the script
does, and a test asserts the set exactly:

```bash
python -c "from scripts.rotate_phi_key import encrypted_columns; [print(t.name, c) for t, c in encrypted_columns().items()]"
```

| table | columns |
|---|---|
| `patients` | `full_name` |
| `notes` | `assessment`, `plan`, `subjective`, `objective` |
| `note_revisions` | `previous_text`, `new_text` |
| `transcripts` | `segments` (JSON) |

**Adding an encrypted column is a change to this runbook.**
`tests/test_key_rotation.py` fails on an exact-set mismatch so that adding
one cannot pass silently.

---

## Environment separation

Each environment has its own key, and the application enforces the part of
that which is enforceable.

The development key is **published on purpose** in `apps/api/.env.example`.
A key committed to a repository protects nothing, so it is treated as
permanently public, and a process with `ENVIRONMENT=production` holding it
**refuses to start**. Publishing it also removes the reason anyone would
copy a real key to a laptop: the fake one already works locally.

A production process refuses to start on any of:

- `PHI_ENCRYPTION_KEY` unset, malformed, or equal to a published repository
  secret — including one hiding in `PHI_ENCRYPTION_KEY_PREVIOUS`;
- the default `JWT_SECRET` or the published object-store secret;
- `REFRESH_COOKIE_SECURE=false`;
- a CORS allow-list still naming `localhost`.

All problems are reported in one message, at import, before the process can
serve a request. Recognising the failure at *boot* rather than at first PHI
write is the point: a key validated lazily lets a process start, pass its
health check, take traffic, and die on the first patient — with whatever it
wrote first already committed under the wrong key.

---

## What Phase 5 still owes

4.1's "TLS everywhere, HSTS, modern cipher suites" splits between the
application and the deployment. The application half is done and tested
(`tests/test_security_headers.py`): HSTS, a strict CSP, `nosniff`,
`DENY` framing, `no-referrer`, and `no-store` on every PHI response, with
no interactive docs in production.

The deployment half is **not** done, and none of it can be asserted from
inside the app:

- **Terminate TLS** in front of FastAPI, with a valid certificate and
  automated renewal. Nothing in this repo serves https.
- **Redirect http to https** at the proxy. The app emits HSTS but cannot
  redirect a first, pre-HSTS request it never receives.
- **TLS 1.2 minimum, 1.3 preferred**, with a modern cipher list — the
  "modern cipher suites" bullet lives entirely here.
- **Set `X-Forwarded-Proto` at the proxy, and make sure nothing else can.**
  The app decides whether to emit HSTS from that header. A proxy that
  forwards a client-supplied value lets a client suppress HSTS.
- **TLS to Postgres, Redis and object storage** as well as to the browser.
  Column encryption means the database sees only ciphertext, but connection
  metadata and every non-PHI column still travel in the clear.
- **Decide on HSTS preload.** `HSTS_PRELOAD` defaults to false because
  submitting a domain to the browser preload list is close to irreversible.
  Turn it on only once the domain and every subdomain are permanently https.

---

## If something goes wrong

**The rewrite failed partway.** Nothing is broken. Both keys are configured
(step 3), so the application reads the mixed database correctly. Fix the
cause and re-run step 5.

**`--verify-only` reports failures after a completed run.** Those rows are
still under the old key. Re-run step 5 — it is idempotent — then verify
again. Do not proceed to step 7 until it is clean.

**A value no key can decrypt.** The run exits non-zero and names the table,
column and row id. Find the key that value belongs to before doing anything
else; do not delete any key while it is outstanding.

**The old key was destroyed too early.** Restore from the backup taken in
step 2 and start over. This is the failure mode the backup exists for, and
the reason step 7 puts key destruction last and gates it on a fresh
verification.

**The key is lost entirely, with no backup.** The encrypted columns are
unrecoverable. What survives is everything unencrypted: encounter and
consent records, audit logs, timestamps, the object store's audio. Treat it
as permanent data loss of the clinical record and escalate to the DPO
immediately — under the Data Privacy Act this is a reportable availability
incident, not merely an outage.
