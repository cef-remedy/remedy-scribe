# Runbook — Secrets management

**Phase:** 4.3 (P0-8) · **Status:** design + what is enforceable today ·
**Last exercised:** 2026-08-28 (inventory and loading path verified against
the code; no production deploy exists yet)
**Related:** [0034](../decisions/0034-an-untested-control-is-a-hope.md),
Phase 4.1's environment-separation decision, [key-rotation](key-rotation.md)

> **Read this first.** There is no deployment yet — Phase 5 chooses the
> hosting target. So the honest scope of this runbook is: *the inventory,
> the loading mechanism, the one thing that has to change at deploy time,
> and the criteria for choosing a manager.* Everything marked **[5.1]**
> below is not done and cannot be done until hosting is decided. Nothing
> here should be read as "secrets are managed."

## 1. The inventory

Every secret the API process needs, ordered by how much it would hurt to
lose one. Names are the environment variables `app/core/config.py` reads.

| Secret | What it protects | Blast radius if leaked | Recoverable? |
|---|---|---|---|
| `PHI_ENCRYPTION_KEY` | Every Fernet-encrypted PHI column: patient names, transcripts | Full plaintext PHI from any database copy or backup | **No.** Lose it and every encrypted column is permanently unreadable |
| `PHI_ENCRYPTION_KEY_PREVIOUS` | The same columns, mid-rotation | The same | A retired key still listed is a retired key still able to read PHI |
| `JWT_SECRET` | Access-token signature | Mint a token for any clinician, any role | Yes — rotating it invalidates every access token at once |
| `S3_SECRET_KEY` / `S3_ACCESS_KEY` | The audio bucket | Every consultation recording ever uploaded | Yes — rotate at the object store |
| `DATABASE_URL` | The Postgres password, inline in the URL | Everything, including the audit log | Yes |
| `GROQ_API_KEY` | ASR and note generation vendor | Vendor spend; not PHI on its own | Yes |
| `ANTHROPIC_API_KEY` | Note generation (the earlier provider path) | Vendor spend | Yes |
| `REDIS_URL` | The Celery broker | Task injection into the pipeline | Yes |

Two of these are different in kind from the rest. `PHI_ENCRYPTION_KEY` is
the only secret whose **loss** is as bad as its **leak**, and so the only
one that must exist in a second place before it is ever used in a first.
`JWT_SECRET` is the only one where rotation is itself the incident
response — see [breach-response](breach-response.md) §4.

## 2. How the app loads secrets, and why that shape is already right

`app/core/config.py` defines `Settings(BaseSettings)` with
`SettingsConfigDict(env_file=".env")`. The property that matters is
pydantic-settings' precedence:

> **process environment > `.env` file > field default**

The application therefore already reads its secrets from **environment
variables**; `.env` is only the development fallback. Any secret manager
that can inject environment variables into a process — which is all of
them — works with **zero application code change**. That is not luck to be
grateful for, it is why this checklist bullet is a deployment task and not
a refactor.

Two consequences to know before planning any rotation:

- `get_settings()` is `@lru_cache`d. Secrets are read **once, at process
  start.** Changing a value in the manager changes nothing until the
  process restarts, so rotation is always a deploy and never a config
  poke. (`app/core/security.py` clears its own derived-cipher cache; that
  does not re-read the environment.)
- Phase 4.1 added a boot-time refusal: `Settings` raises if a production
  process holds a secret whose SHA-256 fingerprint matches one of the
  `_PUBLISHED_DEV_SECRETS` published in this repository. That is the half
  of "production keys never on a developer machine" which is enforceable
  in code, and it works *because* the dev secrets are public — a value
  everyone already knows can be denied by value.

### The one thing that has to change at deploy time

`infra/docker-compose.yml` mounts secrets as a file:

```yaml
  api:
    env_file:
      - ../apps/api/.env
```

That is exactly the pattern the checklist says must not reach a server. A
production compose file (or unit file, or task definition) must **not**
carry `env_file`, and no `.env` may be copied to the host. The replacement
is an injector that fetches the secrets and `exec`s the process with them
in its environment, so the plaintext exists only in that process's memory
and never as a file anyone can `cat`.

`apps/api/.env` is already listed in `apps/api/.gitignore`, so none of this
is about keeping secrets out of git — that part is done. It is about
keeping them off disks.

## 3. Choosing a manager  **[5.1 — not decided here]**

The hosting target is Phase 5.1's call, and it is constrained by data
residency for Philippine health data, which is a question for Legal before
it is a question for engineering. This section gives criteria and a
recommendation, not a decision.

**Requirements, ordered by how quickly they eliminate candidates:**

1. **Residency.** If Legal requires PHI-adjacent key material to stay in
   the Philippines, every hyperscaler manager is constrained to whatever
   region it can offer, and self-hosting may be the only clean answer.
2. **Survives the app being down.** `PHI_ENCRYPTION_KEY` must be
   recoverable when the thing it lives next to is the thing that broke. A
   manager whose only copy is inside the deployment is a single point of
   total, unrecoverable data loss.
3. **Injects environment variables.** Everything qualifies. A floor, not a
   differentiator.
4. **Per-environment isolation with distinct identities.** Staging must not
   be able to read production's secrets. This is what makes 4.1's
   fingerprint check a backstop rather than the only control.
5. **An audit trail of reads.** "Who fetched the PHI key, and when" is a
   question a breach investigation will ask.

**Recommendation for the pilot: SOPS + age, with a written promotion path
to a managed service.**

`sops` encrypts a YAML/env file under an `age` key; the *ciphertext* is
committed to this repository, and the deploy step runs
`sops exec-env secrets.enc.yaml 'docker compose up -d'` so plaintext exists
only in the deployed process's environment. In the checklist's own terms —
"the least infrastructure that meets the compliance bar" — this buys:

- No plaintext secret on any server or laptop.
- Per-environment age keys, so production's key never exists on a
  developer machine (requirement 4), with 4.1's fingerprint check as the
  backstop if it ever does.
- Rotation, and who changed which secret when, as ordinary git history — a
  partial answer to requirement 5: it records writes, not reads.
- No network dependency at boot and nothing new to operate at 2 a.m.
- Requirement 2 satisfied by holding the age private key offline, entirely
  independent of any running system.

**What it does not buy, stated plainly:** no read audit trail, no automatic
rotation, no short-lived dynamic credentials, and it relocates the problem
to custody of one age private key rather than removing it. Those are the
reasons to graduate to **AWS Secrets Manager / GCP Secret Manager / Azure
Key Vault** (if the chosen provider has an acceptable region) or to
**self-hosted HashiCorp Vault or Infisical** (if residency forces it) once
the deployment is real and someone is on call for it. Vault for a
single-clinic pilot is disproportionate: it is a highly available system
whose unavailability takes the application down with it.

**Custody of the age private key** (or of whichever root credential the
chosen manager uses): two offline copies, held by two different people,
neither of them the person who runs deploys day to day. Same requirement as
`PHI_ENCRYPTION_KEY`, same reason — see [key-rotation](key-rotation.md).

## 4. What is true today vs what Phase 5 must do

| | Status |
|---|---|
| Secrets read from the process environment; `.env` only a dev fallback | **Done** — `app/core/config.py` |
| `.env` git-ignored; dev secrets published on purpose so they can be denied by value | **Done** — Phase 4.1 |
| Production boot refuses published dev secrets, a missing PHI key, a non-Secure refresh cookie, localhost CORS | **Done** — Phase 4.1, `_reject_development_secrets_in_production` |
| CI runs with **no secrets at all** (`tests/conftest.py` generates its own PHI key) | **Done** — Phase 4.3, `.github/workflows/ci.yml` |
| A secret manager chosen and provisioned | **[5.1]** Not done. Blocked on hosting + residency |
| `env_file:` removed from the production compose/unit file | **[5.1]** Not done |
| A read audit trail on secret access | **[5.1]** Not done; depends on the manager chosen |
| An automatic rotation schedule per secret | **[5.1]** Not done. Rotation is manual and requires a restart (§2) |

## 5. Verifying which secret a process is actually holding

Without printing it. `app/core/config.py` exposes `secret_fingerprint`, a
truncated SHA-256:

```bash
python -c "from app.core.config import get_settings, secret_fingerprint as f; s = get_settings(); print('jwt', f(s.jwt_secret)); print('phi', f(s.phi_encryption_key or ''))"
```

Those 16 characters can be pasted into an incident ticket freely. Comparing
them across environments answers "is staging using production's key?"
without either value ever being written down.
