# Runbook — Staging environment and its synthetic data

**Phase:** 5.3 · **Status:** the seed script is written and **executed for
real** — against the local Postgres and MinIO on 2026-08-29, with the
output recorded in §6. There is **no deployed staging environment yet**;
Phase 5.1 owns that. What exists today is the dataset and the procedure
for putting it into any database that is not production.
**Related:** [0038](../decisions/0038-ci-gates-what-a-green-check-is-allowed-to-mean.md),
[0029](../decisions/0029-searchable-encrypted-patient-names.md) (why the
names are shaped the way they are),
[0030](../decisions/0030-grounding-is-withheld-not-approximated.md) (the
audio/transcript states the data has to be able to show),
[0031](../decisions/0031-phi-encryption-stays-in-the-application-layer.md)
(published dev secrets), [ci-cd](ci-cd.md),
[secrets-management](secrets-management.md)

> **Staging must never contain production PHI.** Not "should not" — this
> document exists because the moment staging is too empty to reproduce a
> bug, `pg_dump production | psql staging` becomes the obvious next
> command, and under the Data Privacy Act that is an unlogged bulk
> disclosure of every patient in the clinic. The countermeasure is not a
> policy. It is making staging full enough that nobody wants production's
> copy, and making the seed refuse to run against a database that holds
> real accounts.

## 1. Quick reference

```bash
# 1. A database that is not production, migrated to head.
cd apps/api
alembic upgrade head

# 2. Seed it. Both of these are required and neither is in any .env.
export REMEDY_ALLOW_SYNTHETIC_SEED=1
export ENVIRONMENT=staging
python scripts/seed_staging.py            # prompts for the database name
python scripts/seed_staging.py --yes      # non-interactive (CI uses this)
```

Sign in as `doctor@staging.remedy.example`, password
`staging-not-a-real-password`. Also seeded: `compliance@` and `admin@` on
the same domain, same password, so RBAC (decision 0005) can actually be
exercised rather than assumed.

## 2. Why you cannot point this at production

Six locks, checked before a single `INSERT`, each independently
sufficient. They are listed with what each one catches, because a guard
whose purpose is unclear is a guard someone removes:

| # | Lock | Catches |
|---|---|---|
| 1 | `settings.is_production` | `ENVIRONMENT=production` or `prod` |
| 2 | `ENVIRONMENT` allow-list, **failing closed** | `prod-eu`, `staging2`, a typo, an empty value — anything not in `{development, local, staging, test, ci}` is refused, not assumed safe |
| 3 | `REMEDY_ALLOW_SYNTHETIC_SEED=1` | A process that inherited an environment it did not mean to opt into. Deliberately absent from every `.env` in this repository |
| 4 | **Every `clinicians` row must be on `@staging.remedy.example`** | A database holding real accounts |
| 5 | Schema is at the current Alembic head | A half-migrated target, which would otherwise fail partway through with an `UndefinedColumn` traceback |
| 6 | `--yes`, or type the database name | The wrong terminal |

**Lock 4 is the one that actually matters**, because it is the only one
that does not depend on configuration being correct. Locks 1–3 all trust
an environment variable, and the whole class of accident this guards
against is "the environment variables were wrong." Production has real
clinician accounts; the script reads the table and refuses. `.invalid` is
reserved permanently by RFC 2606, so it can never be registered and no
real deploy can match it by coincidence.

Observed, on a database seeded with one ordinary-looking account:

```
Refusing to seed this database:
  - 1 clinician account(s) are not on @staging.remedy.example
    (e.g. 'maria.reyes@remedy.ph'). This database holds real accounts,
    so it is not a staging database. Refusing before writing anything.
```

Before writing anything, the script prints its target — redacted database
URL, `ENVIRONMENT`, the S3 endpoint, and the **fingerprint** of the PHI key
(4.1's `secret_fingerprint`, never the key itself). That is enough to
answer "is staging using the same key as production", which is the
question that matters, without writing either key down.

**Staging should have its own `PHI_ENCRYPTION_KEY`, not production's and
not the published dev one** if the environment is network-reachable. If
the fingerprint printed here ever matches production's, that is a finding,
not a convenience.

## 3. There is no `--reset`, and that is deliberate

The seed **only ever INSERTs**. Running it twice is refused:

```
This database is already seeded (3 synthetic clinician(s)). Running again
would append duplicates, and there is no --reset ...
```

The reason is P0-1. The consent ledger's append-only trigger means seeded
rows **cannot be deleted by anything**, including the table's owner — so a
partial cleanup would leave orphan ledger rows pointing at encounters that
no longer exist. And giving this script the privileges to `DROP DATABASE`
would hand the largest available capability to the exact code path whose
guards just failed.

The only real reset is dropping the database:

```bash
docker compose -f infra/docker-compose.yml exec postgres \
  psql -U remedy -d postgres \
    -c "DROP DATABASE IF EXISTS remedy_staging;" \
    -c "CREATE DATABASE remedy_staging OWNER remedy;"
cd apps/api && alembic upgrade head && python scripts/seed_staging.py --yes
```

Audio objects in the bucket are **not** removed by that. Either let the
lifecycle rule expire them (`AUDIO_RETENTION_DAYS`) or clear the bucket
separately; orphaned objects are harmless — nothing points at them — but
they accumulate across re-seeds.

## 4. What is in the dataset, and why each row is there

Nothing here is filler. The directory is built to break the fuzzy matcher
and the encounters are built to reach every state the UI has a rendering
for. A seed that only produces the happy path is how a staging environment
comes to look healthy while hiding every interesting case.

### 14 patients — the matcher's failure modes, by name

The matcher is `difflib` over decrypted names (decision 0029) and all of
its interesting cases are naming-convention cases. Every name is invented.

| Name | Exists to exercise |
|---|---|
| Maria Concepcion Dela Cruz | Compound surname — a doctor types "Maria Dela Cruz" (**observed: hit at 0.73**) |
| Maria Santos Cruz | Deliberate near-collision with the above; both must be returned and ranked, neither deduped into the other (**0.75**) |
| Ma. Cristina Reyes-Bautista | `Ma.` abbreviation + hyphenated married surname. "Maria Bautista" shares **no** token with it and **does not** return it — the honest recall limit of the current matcher, kept visible |
| Jose Antonio Bautista Jr. / Jose Antonio Bautista | Father/son pair differing only by `Jr.`; **only the birthdate** distinguishes them — P0-6's "name + birthdate, never name alone" in two rows |
| Juan Miguel Dela Cruz (2019) | Paediatric; shares a surname with a household member. The consent roster records a guardian, not the patient |
| Rosario Pena Villanueva | Stored unaccented, as a keyboard produces it. Dictated "Peña" scores **0.96 → near match, one-tap confirmation** |
| Ana Marie Santos ×2 (1988, 1996) | Identical name, different birthdates. The case where "exact match links silently" **must not** fire. Verified: same name + wrong birthdate → `none`; + right birthdate → `exact` |
| Wilson Tan Sy | Chinese-Filipino short surnames; difflib ratios are jumpy on short tokens ("Wilson Sy" → **0.82**) |
| Nur-Aisha Abdulkadir Mangudadatu | Maguindanaon naming; longest name in the set (32 chars) |
| Juanito Ramos Aquino | Legal name of someone called "Jun". "Jun Aquino" → **0.67**: above the 0.55 search threshold, below the 0.82 dedup threshold — the band where having two thresholds is what makes the flow work |
| Ferdinand Emmanuel Salazar III | Roman-numeral suffix; oldest patient (77), so a date widget defaulting to a recent decade shows itself |
| Precious Grace Ocampo | Not Spanish-derived, so the token prefilter is not flattered |

### 4 Taglish consultations

Lower respiratory infection, hypertension follow-up, dermatology, and
paediatric fever. Word-level timings and confidences, with deliberately
low-confidence words on **clinically load-bearing** tokens (a dose
frequency, a lab name, an episode count) so the `[INAUDIBLE]` suppression
path (P0-4) is visible in staging rather than only in unit tests.

### 11 encounters — every state the UI can render

| State | Why it is in the set |
|---|---|
| signed, audio playable, **cosmetically edited** | The subtle half of decision 0030: a same-length edit leaves `spans_fit` **True** while `edited_since_generation` goes True. Nothing else shows those two flags disagreeing |
| generated, **grounding withdrawn by a rewrite** | The headline case: an insertion breaks the offsets, `spans_fit` goes **False**, and the UI must render plain text and say why |
| authenticated / filed | Notes parked mid-state machine |
| consent **withdrawn** after signing | Note stays in the record; audio and transcript purged. Grounding must say `withdrawn`, not `expired` |
| audio **expired** under retention | The other rung. Observably identical to withdrawal, legally not |
| never recorded | A third distinct rung, and not a deletion |
| consent **declined** | `blocked_no_consent`, terminal, never retried (decision 0002) |
| **loose session**, no patient linked | P0-6: recording is never blocked on identity. Also why this note cannot be filed — `note_lifecycle` refuses |
| transcription_failed / generation_failed | Phase 1.5's two terminal stages, with retry counts and vendor-only error text |

Note statuses are driven through `note_lifecycle.transition`, never by
assigning `Note.status`. Seeding around the state machine would produce a
dataset the application itself could not have created.

## 5. Why the notes' citations actually resolve

The note bodies are built by calling the **production** span builder,
`app/services/note_generation/shared.py:build_sections` — not a local copy
of its convention.

`apps/web/smoke/seed_pipeline.py` duplicates that convention on purpose,
and for a smoke test that is the right trade: the duplicate is a canary.
For a dataset people will trust for weeks it is the wrong one. Change the
join separator and a duplicated version silently produces notes whose
grounding never lines up — and staging becomes *worse* than empty, because
it looks fine.

The seed then **verifies rather than asserts**: every note is read back
through `resolve_grounding`, and a note with a live transcript that
resolves zero cited segments fails the run. Skip it with `--no-verify`,
which prints "the dataset is unproven" — because that is what it is.

## 6. Executed — 2026-08-29, local Postgres 16 + MinIO

`ENVIRONMENT=staging`, fresh `remedy_staging` database at head
`a0b1c2d3e4f5`, real audio uploaded over the presigned-multipart path.

```
clinicians       3
patients        14
encounters      11
consent_entries 12
transcripts      8
notes            8
revisions        2
audio_objects    7
```

Grounding read-back, all eight notes:

```
signed         audio=available      transcript=available   cited=6 (+3 ctx)  fit=[assessment,plan,subjective]           edited=[plan]        suppressed=[objective]
authenticated  audio=available      transcript=available   cited=6 (+2 ctx)  fit=[all four]                             edited=[]            suppressed=[]
filed          audio=available      transcript=available   cited=5 (+2 ctx)  fit=[all four]                             edited=[]            suppressed=[]
generated      audio=available      transcript=available   cited=6 (+3 ctx)  fit=[objective,plan,subjective]            edited=[assessment]  suppressed=[]
signed         audio=withdrawn      transcript=withdrawn   cited=0 (+0 ctx)  fit=[all four]                             edited=[]            suppressed=[]
signed         audio=expired        transcript=available   cited=5 (+2 ctx)  fit=[all four]                             edited=[]            suppressed=[]
generated      audio=available      transcript=available   cited=6 (+3 ctx)  fit=[assessment,plan,subjective]           edited=[]            suppressed=[objective]
generated      audio=never_recorded transcript=available   cited=6 (+3 ctx)  fit=[all four]                             edited=[]            suppressed=[]

OK: every seeded note with a live transcript resolves at least one cited segment.
```

Both decision-0030 states landed as intended: row 1 has `plan` **edited
and still fitting** (same-length substitution), row 4 has `assessment`
**edited and no longer fitting** (insertion).

Verified separately against the same database:

- **All 7 uploaded audio objects resolve via `head_object`** (896,044-byte
  `audio/wav` each), so playback is genuinely available rather than a
  database claim.
- `audio_object_key` is set on exactly **9** encounters (7 uploaded + the
  expired and withdrawn rows) and `audio_deleted_at` on exactly **2** —
  no encounter claims bytes that were never written. See §7.
- **All 14 patient names decrypt** through `EncryptedString`, and the raw
  column holds Fernet ciphertext (`gAAAAABqkl0n...`).
- Consent ledger: 10 `given`, 1 `declined`, 1 `withdrawn`.

## 7. The bug this seed created, and had to be fixed for

Worth recording because it is the **second** time this system produced a
row asserting bytes that were never there, and both times the row looked
completely ordinary.

Under `--no-audio`, the first version still wrote an `audio_object_key`
for uploads it had skipped. `grounding._audio_state` treats "the row
claims a key, storage says 404" as proof the bucket lifecycle rule expired
the object, and stamps `audio_deleted_at` — which is *correct* in
production, and is exactly the verification Phase 3 added because the
database's belief about audio is not evidence (decision 0030 §2). Handed a
key for an upload that never happened, it recorded a **retention expiry
that never occurred**.

Caught by rehearsing the CI job, not by reading the code: the read-back
reported `audio=expired` where `never_recorded` was expected. Fixed by
making those encounters honestly never-recorded — no key, no bytes, no
claim. The expired and withdrawn scenarios still carry a key with no
object behind it, which is legitimate there and only there, because it is
paired with the deletion stamp that explains it.

## 8. What does not exist yet

- **A deployed staging environment.** Phase 5.1. This runbook seeds a
  database; it does not stand one up.
- **A staging `PHI_ENCRYPTION_KEY` distinct from the published dev key.**
  Required before staging is reachable from anywhere but a laptop (§2).
- **Object-storage cleanup on re-seed** (§3).
- **Any automatic refresh.** The dataset is static. If staging drifts (a
  tester signs everything), the reset in §3 is the whole story.
- **A check that production has never been dumped into staging.** The
  locks prevent the *seed* from touching production; nothing prevents
  someone restoring a production dump into staging by hand. That is a
  people-and-access control, and it belongs with the DPO alongside the
  backup retention question in [backup-restore](backup-restore.md) §6.
