# 0031 — PHI encryption stays in the application layer, and the DPO gets the final call

**Phase:** 4.1 · **Decided by:** implementation · **Date:** 2026-08-28

## The 🧠 as posed

`docs/tech-stack.md` §3 specified `pgcrypto`. The implementation used
app-layer Fernet, because it works identically on SQLite and the test suite
runs on SQLite. The checklist's position is that the divergence is fine but
"should be a decision, not an accident," and it names a third option —
envelope encryption against a managed KMS — as "the answer that makes
rotation tractable and keeps keys off your servers."

This decision resolves the *implementation* question. It does not resolve
the *policy* question, and the distinction matters enough to state first.

## What is not being decided here

**The final call belongs to Remedy's DPO.** The checklist says to ask them
before wider rollout because retrofitting is much worse than starting
there, and that is right. Three things they may require that no amount of
engineering judgment here can substitute for:

- **Keys in an HSM or managed KMS**, so no human and no application process
  ever holds key material. That is a compliance posture, not a performance
  one, and it is the kind of thing a Data Privacy Act audit asks about
  directly.
- **Key custody split from application custody** — whoever can deploy the
  app should not automatically be able to read PHI. Today they can: the key
  is an environment variable in the same deployment the app lives in.
- **A documented rotation cadence.** Rotation is now possible (below) and
  measured. Whether it must happen annually, on personnel change, or only on
  suspected compromise is a policy question with a real cost attached, and
  the cost is now a number rather than a guess.

What follows is what the implementation does *until* that answer arrives,
chosen so that the answer, whichever it is, is not made harder.

## The three options, at this pilot's actual scale

### `pgcrypto` — what the tech stack specified

`pgencrypt`/`pgp_sym_encrypt` run inside Postgres. In exchange:

- **The key travels to the database on every query.** `pgp_sym_encrypt(x,
  'key')` puts the key in the SQL statement — which means in
  `pg_stat_statements`, in the slow-query log, and in any statement log the
  DBA turns on for an afternoon. Avoiding that needs care that is easy to
  get wrong and invisible when you do.
- **The test suite loses its database.** Every test in this repo except the
  `postgres`-marked ones runs on SQLite for speed, and `pgcrypto` has no
  SQLite equivalent. Phase 0.5 already documented what happens when tests
  and production diverge; this would move *encryption itself* into the
  divergent set, which is the worst possible thing to only test in the
  slow lane.
- **It does not solve rotation.** `pgcrypto` has no key registry and no key
  id in its output either. Rotating still means `UPDATE` every row. The
  cost this decision measures is not avoided by moving the same symmetric
  primitive inside the database.

Its one genuine advantage — the application process never holds the key —
is real, but it is much better delivered by KMS than by `pgcrypto`.

### KMS envelope encryption — the right long-term answer

A data key per row (or per encounter), itself encrypted under a key the
application can never read. Rotating the *master* key then re-wraps data
keys instead of rewriting PHI, which is the property that makes rotation
cheap at any data volume.

It is also the option that answers all three of the DPO questions above at
once. It is not being built now because:

- It needs a KMS the clinic operates and pays for, plus IAM, plus a story
  for what happens when it is unreachable mid-consultation.
- Every read becomes a decrypt call unless data keys are cached, and caching
  them re-introduces the key-in-process property that was the point.
- At the measured scale below, the thing envelope encryption buys — cheap
  rotation — costs **three minutes** today.

That last point is the honest reason. Envelope encryption is not
over-engineering in general; it is over-engineering for a database whose
entire rotation completes in the time it takes to read this document.

### App-layer Fernet — kept, and now with the missing half built

Fernet is AES-128-CBC + HMAC-SHA256 with a random IV per token, in a
maintained, audited library. Encryption happens before the value reaches
the driver, so ciphertext is all the database, its logs, its backups, and
its replicas ever see — a strictly stronger property than `pgcrypto`, which
sees plaintext arguments.

The reason to keep it is not that it was already there. It is:

- **The same code path in every environment.** SQLite and Postgres get
  byte-identical treatment, so the fast test suite tests the real thing.
- **It does not block the KMS move.** The column contract is "opaque
  ciphertext in a text column." Envelope encryption produces opaque
  ciphertext in a text column too. Migrating is a re-encryption pass over
  every row — which is *exactly* the script this phase built and rehearsed.
  The retrofit the checklist warns about is expensive when there is no
  rewrite tooling; there now is, and it has a measured runtime.

## What was missing, and is now built

The checklist's ⚠️ was accurate: there was no rotation story at all.

**`MultiFernet` is the mechanism** (verified against cryptography 43.0.1's
own source, not assumed): `encrypt` always uses `_fernets[0]`; `decrypt`
tries each key in turn; `rotate` decrypts under whichever key works and
re-encrypts under the first, preserving the token's original timestamp.

So `PHI_ENCRYPTION_KEY` is the key that writes and
`PHI_ENCRYPTION_KEY_PREVIOUS` is a decrypt-only list, and the database is
allowed to hold a mix of both at once. That is what makes the rewrite an
online operation rather than an outage, and what makes an *interrupted*
rewrite a non-event instead of a data-loss incident.

`scripts/rotate_phi_key.py` performs the rewrite. Two properties are worth
naming, because both are ways the obvious version silently destroys data:

- **Columns are discovered by type, never listed.** A hand-written list
  goes stale the first time someone adds a model, and a column missed by a
  rotation is unreadable forever once the old key is deleted. A test asserts
  the discovered set *exactly*, so adding an encrypted column fails loudly
  until someone confirms the rotation covers it.
- **Verification is a separate pass under the new key alone.** A rotation
  that raised no error is not evidence every row moved. Only a full read
  that the old key cannot satisfy is. `--verify-only` is the gate on the one
  irreversible step, deleting the old key.

## The cost, measured rather than estimated

Rehearsed end to end against a real Postgres 16, seeded with **17,500 rows
holding 35,000 encrypted values and 1.73 GB of ciphertext** — roughly six
months of a clinic running 40 consultations a day:

| table | rows | values | time |
|---|---|---|---|
| `patients` | 5,000 | 5,000 | 5.1 s |
| `notes` | 5,000 | 20,000 | 7.5 s |
| `note_revisions` | 2,500 | 5,000 | 3.6 s |
| **`transcripts`** | **5,000** | **5,000** | **164.8 s** |
| **total** | **17,500** | **35,000** | **181.0 s** |

**Transcripts are 14% of the values and 91% of the time.** The cost of a
rotation is not row count, it is bytes: a 15-minute word-level transcript is
~340 KB of ciphertext, and the other three tables together are 16 MB against
the transcript table's 1.71 GB.

**And the cost is not the cryptography.** The same scan, run three ways over
the same data:

| what ran | time |
|---|---|
| read + decrypt every value (`--verify-only`) | 16.5 s |
| read + decrypt + re-encrypt, no writes (`--dry-run`) | 26.7 s |
| read + decrypt + re-encrypt + `UPDATE` (full run) | **181.0 s** |

Fernet accounts for **27 s of 181 s — 15%**. The remaining 85% is Postgres
rewriting TOAST-ed text one row per statement. This is the second time on
this codebase that a measurement has moved the blame off decryption
(decision 0029 found ORM hydration, not decryption, dominating patient
search), and it matters for the option comparison: **neither `pgcrypto` nor
a KMS would make a rotation meaningfully faster**, because none of them
change the number of bytes Postgres has to write. Only retention does.

That reframes the checklist's warning usefully. "Re-encrypting every row"
sounds like an O(rows) problem to be feared as the patient directory grows;
it is really an O(recorded minutes) problem, and it grows with the retention
window, not the practice. **Phase 4.4's retention deletion is therefore also
a rotation-cost control** — expired transcripts are rows that never have to
be rewritten again.

Three minutes, online, at six months of data. That is the number the
"tractable rotation" argument for KMS has to beat, and at pilot scale it
does not.

## Separate keys per environment, made enforceable

"Production keys never on a developer machine" is the kind of bullet that
gets written down and then not enforced, because a Fernet key is 32 random
bytes and no inspection can tell a production one from a local one.

So the problem is inverted: **the development key is published on purpose**,
in `.env.example`, and its fingerprint is denied by value. A process with
`ENVIRONMENT=production` holding it refuses to start, along with the default
`JWT_SECRET`, the published object-store secret, a non-`Secure` refresh
cookie, and a CORS allow-list still naming localhost. Every problem is
reported at once, at import, before the process can take a single request.

Publishing the dev key also removes the *motive* for the behaviour being
banned. Nobody needs to copy a real key down to get a working laptop,
because the fake one already works.

The refusal is deliberately at boot rather than at first use. A key
validated on first PHI write means the process starts, passes its health
check, takes traffic, and dies on the first patient — with whatever it wrote
first already committed under the wrong key.

## What would change my mind

- **The DPO requiring HSM-held or KMS-held keys.** This is the expected
  outcome of asking, not a remote possibility, and it is a policy
  requirement no measurement here answers. The migration path is a
  re-encryption pass, which now exists and takes minutes.
- **Rotation crossing roughly half an hour.** At the measured ~10 MB/s of
  transcript ciphertext that means ~18 GB, or a few years of retained
  recordings. Envelope encryption's cheap master-key rotation starts to earn
  its infrastructure there. Watch the `transcripts` table's size, not the
  patient count.
- **Key custody needing to be split from deploy access.** Today anyone who
  can deploy can read the key and therefore all PHI. No amount of rotation
  discipline fixes that; only a KMS does.
- **A second service needing to read the same columns.** App-layer
  encryption means every reader needs the key and the same library. Two
  readers is the point at which centralising decryption starts to look
  better than distributing a key.
