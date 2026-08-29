# Runbook — Deployment

**Phase:** 5.1 · **Status:** ⚠️ **procedure written and locally rehearsed;
never executed against a host** · **Last exercised:** 2026-08-29 (compose
files rendered, images built and inspected, readiness probe driven against
real Postgres/Redis including forced outages — see §8 for exactly what that
did and did not cover)
**Related:** [0036](../decisions/0036-one-vm-and-one-bought-database.md),
[secrets-management](secrets-management.md), [backup-restore](backup-restore.md),
[key-rotation](key-rotation.md), [breach-response](breach-response.md)

> **Read this first.** There is no deployment target. No VM has been rented,
> no domain registered, no certificate issued, no managed database
> provisioned. Every command below is written against files in this
> repository that parse, build and validate — and none of them has been run
> against a server. Phase 4.3 set this precedent for the secrets runbook and
> it applies here more strongly: **nothing in this document should be read
> as "the system is deployed."** §8 lists what was actually executed.

## 1. The topology, in one screen

One Linux VM. Docker Compose. Five long-lived containers plus a one-shot.

```
                      internet
                         |  443 (and 80, redirect only)
                +--------v---------+
                |      edge        |   Caddy: TLS termination, ACME,
                |   10.89.7.10     |   the built PWA on /, proxy on /api
                +---+----------+---+
                    |          |
        +-----------v--+   +---v-----------+
        |     api      |   | (static /srv) |
        | uvicorn x2   |   +---------------+
        +--+--------+--+
           |        |        +----------+   +----------------+
           |        +-------->  redis   |   |     worker     |
           |                 | (on VM)  <---+  celery x4     |
           |                 +----^-----+   +-------+--------+
           |                      |                 |
           |                 +----+-----+           |
           |                 |   beat   |  EXACTLY ONE
           |                 +----------+           |
           |                                        |
    +------v----------------------------------------v-------+
    |  MANAGED POSTGRES (external, automated backups + PITR) |
    +--------------------------------------------------------+
    +--------------------------------------------------------+
    | OBJECT STORAGE (external — PROVIDER BLOCKED ON LEGAL)  |
    +--------------------------------------------------------+
```

Files:

| | |
|---|---|
| `infra/docker-compose.prod.yml` | The production topology. Not `infra/docker-compose.yml`, which is dev-only and says so. |
| `infra/caddy/Caddyfile` | TLS floor, http→https, the forwarded headers, the static-asset cache policy, the CSP for the HTML surface. |
| `infra/env/production.env.example` | Template for the **sops document**. Never fill in and leave on disk. |
| `infra/deploy.sh` | The deploy. Gates the long-running services on the migration succeeding. |
| `apps/api/Dockerfile` | API, worker and beat — one image, three commands. |
| `apps/web/Dockerfile` | Builds the PWA and bakes it into the Caddy image. |

## 2. What has to exist before the first deploy

Ordered so that the blocking item is first. Do not start at step 3.

1. **⚠️ Legal answers the residency question.** Decision 0036's opening
   section lists the four questions and why each one eliminates options.
   Every hostname in `production.env.example` implies a jurisdiction. **Do
   not provision anything until this is answered** — a managed Postgres in
   the wrong region holding real PHI is not fixable by migrating later; the
   data was already there.
2. **A DPO and a breach response team exist.** Both are legally required and
   both are currently empty (Phase 4.3 shipped that roles table empty on
   purpose). The person who answers the phone at 3 a.m. is a prerequisite
   for the pilot, not a follow-up.
3. **A VM**, in the region Legal named. 2 vCPU / 4 GB is ample for this
   workload; the sizing constraint is Groq's quota, not this host.
   Firewall: **inbound 22 (key-only, from a known address) plus 80 and 443,
   and nothing else.** The unpublished container ports are what make
   `X-Forwarded-Proto` trustworthy (§7) — a firewall that exposes 8000
   deletes that guarantee.
4. **A managed Postgres** meeting §3.
5. **Object storage** meeting §4.
6. **A domain**, an A/AAAA record pointing at the VM, and an `ACME_EMAIL`
   that a human reads.
7. **The secrets**, encrypted per [secrets-management](secrets-management.md)
   §3 and `infra/env/production.env.example`.
8. **`PHI_ENCRYPTION_KEY` escrowed in two offline copies held by two
   different people** — *before* it encrypts a single row. This is the one
   secret whose loss is as bad as its leak, and under the Data Privacy Act
   losing it is a reportable availability incident, not an outage.

## 3. Managed Postgres — the acceptance criteria

The checklist bullet is "automated backups and PITR". That phrase is worth
turning into things you can check on a provider's console, because most of
them will tick "backups" and mean something weaker.

| Requirement | Why | How to verify before trusting it |
|---|---|---|
| **Continuous WAL archiving with PITR to any second in the window** | A nightly dump means the worst case is a clinic day of unrecoverable notes. [backup-restore](backup-restore.md) says the same in the same words and marks it **[5.1]**. | Restore to a timestamp *in the middle of a day*, not to a nightly boundary. A provider that only offers restore-to-snapshot is not offering PITR. |
| **PITR window ≥ 7 days** | Discovering a bad migration takes longer than 24 hours. | Read the plan's actual limit, not the marketing page. |
| **Backups encrypted at rest, in a different failure domain from the primary** | A backup on the same disk is a copy, not a backup. | Ask where. |
| **`sslmode=verify-full` works, with a downloadable CA** | The 4.1-owed "TLS to Postgres" leg. `require` encrypts without authenticating — it stops a passive eavesdropper and not an active one, and every non-PHI column (birthdates, clinician emails, the entire audit log and consent ledger) travels in the clear on this connection. | Connect with `verify-full` and a deliberately wrong `sslrootcert`. It must fail. If it connects, `verify-full` is not doing what it says. |
| **Postgres 16+** | Matches the dev image and the tested migration chain. | — |
| **A restore drill, by a second person, before the pilot** | Decision 0034: an untested control is a hope. [backup-restore](backup-restore.md) §6 says plainly that nobody but its author has ever run it. | Schedule it. Put the result in that runbook's §5, including the failures. |

**⚠️ The two-artifact rule applies to the managed service too.** The database
dump and `PHI_ENCRYPTION_KEY` must never travel or rest together. A provider
backup is a pile of ciphertext in the columns that matter; the same backup
plus the key is a complete, decryptable copy of every patient record in one
file. Do not put the key in the same cloud account's secret store as the
database backups without deciding, deliberately, that you have.

## 4. Object storage — the acceptance criteria

**⚠️ Provider blocked on Legal.** Whatever it turns out to be:

- **Lifecycle expiration must actually be enforced**, not merely accepted.
  Decision 0033 makes the bucket lifecycle rule one of the two layers of
  retention enforcement, and decision 0014 records a MinIO version that
  accepted a lifecycle `PUT`, never echoed the abort action back, and left
  it unverifiable whether it was enforced at all. **Before the pilot: leave
  a real multipart upload incomplete past
  `S3_ABORT_INCOMPLETE_UPLOAD_AFTER_DAYS` and confirm it is gone.** That
  check has never been run against anything.
- **Server-side encryption at rest**, and **TLS on the endpoint** — this is
  where consultation audio travels.
- **Versioning or replication**, because [backup-restore](backup-restore.md)
  §1 has object-storage backup as an open **[5.1]** item, and a restored
  database referencing audio that no longer exists reports `expired` for
  recordings that were never expired (Phase 3's `_audio_state`).
- **The browser can reach it directly.** Presigned URLs are fetched by the
  page (decision 0013 for upload, 0030 for playback), so `S3_PUBLIC_ORIGIN`
  goes into the page's CSP `media-src`/`connect-src`. Get it wrong and
  playback fails silently with only a console message.

## 5. Secrets, and the one thing that changes at deploy time

This implements [secrets-management](secrets-management.md) §2-3 rather than
inventing anything: **sops + age**, ciphertext committed, plaintext only ever
in a process environment.

```bash
# once, per environment
cp infra/env/production.env.example infra/env/production.env
$EDITOR infra/env/production.env                 # fill in; see the file's own notes
sops --encrypt --age "$AGE_RECIPIENT" \
     infra/env/production.env > infra/env/production.enc.env
shred -u infra/env/production.env                # the plaintext must not survive this line
```

`infra/docker-compose.prod.yml` has **no `env_file:` anywhere**, which is the
one concrete instruction the secrets runbook left to this phase. Values reach
the containers through the process environment of the `docker compose`
invocation, which `sops exec-env` supplies.

**`infra/.env` must not exist.** Docker Compose auto-loads it for variable
interpolation, which silently puts the secrets back on disk in a way nobody
reviewing the compose file would see. `infra/deploy.sh` refuses to run if it
finds one, and deliberately does not delete it — if it holds the only copy of
a secret, deleting it *is* the incident.

Custody of the age private key: two offline copies, two different people,
neither of them the person who runs deploys day to day.

## 6. The deploy

```bash
git checkout <release-tag> && git pull
sops exec-env infra/env/production.enc.env 'infra/deploy.sh'
```

`infra/deploy.sh` is commented step by step; what it guarantees is:

| Step | What it does | Why it is a step and not a side effect |
|---|---|---|
| 0 | Refuses a dirty working tree; refuses `infra/.env`; derives `REMEDY_RELEASE` from the git sha | An image tagged with a sha it does not contain makes "what is running in the clinic?" unanswerable |
| 1 | `docker compose config >/dev/null` | Every required value is `${VAR:?message}`, so this is a complete presence check **before** anything is built or stopped. Output is discarded because a rendered production compose file contains every secret in plaintext |
| 2 | Builds both images | `npm run build` runs `tsc -b`, so a client/server shape mismatch fails here rather than in a clinic |
| 3 | `alembic history --indicate-current` | Prints the schema change **before** applying it, to a database holding real patient records. An empty diff on a release that was supposed to add a column means the migration was never committed |
| 4 | **`alembic upgrade head`, gated** | See below |
| 5 | `docker compose up -d --remove-orphans` | Cannot start `migrate` — it carries a profile |
| 6 | Polls `/ready` until 200, with a 120 s deadline | "Five containers are running" is not "this instance can serve traffic" |
| 7 | Prints the checks a script should not pretend to do for you | §7 |

### Why the migration is a service with a profile

Checklist 5.1: *"run migrations as an explicit deploy step, never
automatically on boot."* Making that structural rather than remembered:

`migrate` carries `profiles: ["deploy"]`. Compose skips profiled services on
a plain `docker compose up -d` — the command someone will actually type at
2 a.m. — so a restart cannot drag a migration along with it. Verified:

```
$ docker compose -f infra/docker-compose.prod.yml config --services
api  beat  edge  redis  worker

$ docker compose -f infra/docker-compose.prod.yml --profile deploy config --services
api  beat  edge  migrate  redis  worker
```

Three long-lived services start from the same image. A migration in an
entrypoint means three processes racing for the same Alembic lock on every
restart, and a container that OOM-restarts at 3 a.m. quietly applying a
schema change nobody triggered.

**Before a destructive migration, note the pre-migration UTC timestamp that
step 4 prints.** With PITR that timestamp is your recovery point, and it is
only useful if it was written down before the schema changed.

If step 4 fails, **nothing has been restarted** — the previous release is
still serving. Fix forward, or restore to that timestamp. Do not start the
new images against a half-migrated schema.

## 7. Verifying a deploy

Step 6 of the script proves the API can reach Postgres and Redis. It proves
nothing else. These are the checks that need a human to look at the result:

1. **TLS, from outside the VM.**
   ```bash
   curl -sSI https://$REMEDY_PUBLIC_DOMAIN/health    # 200 + strict-transport-security
   curl -sSI http://$REMEDY_PUBLIC_DOMAIN/ | head -1 # 301
   nmap --script ssl-enum-ciphers -p 443 $REMEDY_PUBLIC_DOMAIN  # no TLSv1.0/1.1
   ```
   Caddy's defaults should give you all three. The point of checking is that
   "should" is not "does".

2. **A client cannot set `X-Forwarded-Proto`.**
   ```bash
   curl -sSI https://$REMEDY_PUBLIC_DOMAIN/health -H 'X-Forwarded-Proto: http'
   ```
   HSTS must still be present. Note the nuance: in production
   `SecurityHeadersMiddleware` emits HSTS regardless of the header, so this
   tests the proxy rather than the app — the app-side hole is real only in a
   non-production environment served over TLS, where a client can suppress
   HSTS on its own response. The header still has to be authoritative,
   because anything added later that trusts it (URL building, redirects,
   logging) inherits a client-controlled value.

3. **The login rate limit is per IP, not per clinic.** Fail a login five
   times from one machine; confirm a second machine can still log in. If it
   cannot, `--proxy-headers` / `--forwarded-allow-ips` is wrong and every
   doctor shares one bucket. See the `X-Forwarded-For` comment in
   `infra/caddy/Caddyfile` — and note that `--forwarded-allow-ips=*` is
   *worse than nothing*, because uvicorn 0.30.6 then takes the leftmost,
   client-supplied entry, which an attacker can rotate per attempt.

4. **Exactly one beat.**
   `docker compose -f infra/docker-compose.prod.yml ps beat`.
   Two would double-fire the hourly retention purge, which deletes PHI.

5. **A real upload round trip.** The only thing that proves object storage
   works — readiness deliberately does not check it (§9). Record a short
   consultation in the browser, confirm the object lands in the bucket,
   confirm the note generates.

6. **The PHI key decrypts what was already there.** Open a patient created
   before this deploy. A wrong key gives `InvalidToken`, and by the time you
   notice it will have written new rows under it.
   ```bash
   docker compose -f infra/docker-compose.prod.yml exec -T api python -c \
     "from app.core.config import get_settings, secret_fingerprint as f; print(f(get_settings().phi_encryption_key or ''))"
   ```

### If the API container will not start

Read the log before anything else. Phase 4.1 added a boot-time refusal, and
its message begins:

```
Refusing to start with this configuration (ENVIRONMENT='production'):
  - PHI_ENCRYPTION_KEY is the key published in apps/api/.env.example. ...
  - REFRESH_COOKIE_SECURE is false. ...
  - CORS_ALLOW_ORIGINS still contains a localhost origin ...
```

**That is the guard working, not a bug in the deploy.** It refuses a
production process holding a secret published in this repository, a missing
PHI key, a non-Secure refresh cookie, or a localhost CORS origin. It reports
all problems at once, because a deploy with one of these wrong usually has
several. Fix the secret in sops and redeploy — do not weaken the setting.

It fails at boot rather than at first use on purpose: a key validated only
when a column is first written means the process starts, passes its health
check, takes traffic, and dies on the first patient — with the wrong key
already committed to whatever rows it managed to write first.

## 8. ⚠️ What was actually executed, and what was only written

Stated plainly, because a config file must never imply a deployed control.

**Executed, on a developer machine, 2026-08-28/29:**

| | Result |
|---|---|
| `docker compose -f infra/docker-compose.yml config` | passes |
| `docker compose -f infra/docker-compose.prod.yml config`, nothing set | fails, naming the missing variable — the `${VAR:?}` guard works |
| the same with all variables set | renders; only 80/443 published, both from `edge` |
| `config --services`, with and without `--profile deploy` | 5 services vs 6 — `migrate` is genuinely unreachable from a plain `up` |
| `--scale beat=2` (reproduced on a minimal compose file) | warns and starts **one**; `--scale worker=3` starts three |
| `caddy validate` on `infra/caddy/Caddyfile` | **Valid configuration** |
| `caddy fmt --diff` | clean |
| `shellcheck infra/deploy.sh`, `bash -n` | clean |
| `docker compose build api` | builds; runs as uid 10001, contains no `.env` and no `.venv`, `/var/lib/remedy-beat` owned by `remedy` |
| the same build **without** `.dockerignore` | image contains `/app/.env` carrying a working `PHI_ENCRYPTION_KEY` — the defect this phase found |
| `vite build` with `VITE_API_BASE_URL=/` | bundle contains **zero** occurrences of `localhost:8000`; requests compile to relative `/api/v1/...` |
| the readiness probe against real Postgres, Redis and MinIO, with dependencies stopped and restarted | §9 |

**Written and never executed:**

- **Nothing has been deployed.** No VM, no domain, no certificate, no
  managed Postgres, no object-storage account. `infra/deploy.sh` has never
  been run end to end; only its individual compose commands have.
- **No TLS handshake has happened.** The Caddyfile parses. Caddy has never
  served a request with it, never obtained a certificate, and the cipher
  list has never been negotiated with a browser. The `nmap` check in §7 is a
  check to run, not a result.
- **`sops` was not run**, and no age key exists. The secrets mechanism is
  the one the secrets runbook designed; this phase wired the compose file
  and the deploy script to it and stopped there.
- **The managed Postgres does not exist**, so PITR is a specification in §3
  and not a tested recovery path. The only tested RPO on this project
  remains "whenever the last dump ran" ([backup-restore](backup-restore.md) §6).
- **The `X-Forwarded-Proto` and `X-Forwarded-For` behaviour was reasoned
  from Caddy's and uvicorn 0.30.6's source and validated as config — not
  observed on the wire.** §7's checks 2 and 3 are what would actually
  establish it, and they need a running edge.
- **The web image was not built.** `apps/web/Dockerfile` is unbuilt; the
  Vite build inside it was run directly on the host instead, which proves
  the build and the base-URL behaviour but not the Dockerfile.
- **Neither Celery role was started from the production compose file.**

## 9. Health and readiness — what the probes mean

`GET /health` and `GET /ready`, both unauthenticated, both unversioned (an
orchestrator has no credential, and a probe path that moves at an API version
bump un-monitors a deployment quietly). Implementation and full reasoning:
`apps/api/app/api/routes/health.py` and decision 0036 §7.

| | `/health` (liveness) | `/ready` (readiness) |
|---|---|---|
| Question | Is this process wedged? | Can this instance serve traffic? |
| Failure means | **Restart the container** | **Take it out of the load balancer, leave it running** |
| Checks | Nothing external | Postgres `SELECT 1` through the app's own pooled session; Redis `PING`, 2 s timeout |
| Wired to | `healthcheck:` in `infra/docker-compose.prod.yml` | `health_uri /ready` in `infra/caddy/Caddyfile`, `health_timeout 10s` |

**Liveness checks nothing external on purpose.** A liveness probe that
touches Postgres restarts a healthy application because the database
blinked — and it restarts every replica at once, because they share the
dependency. A 30-second dependency outage becomes a crash loop that outlives
its cause.

**Object storage is deliberately not in readiness.** With the bucket down,
uploads and playback fail — but consent capture, worklists, patient matching,
review and signing all work, and P0-2's offline queue exists so a doctor
keeps recording through it. Gating traffic on it would convert a partial
outage into a total one. It is also a slow remote call wrapped in botocore's
retry, so paying it every ten seconds risks causing that total outage by
accident. Object-storage reachability is Phase 5.2's monitoring concern; the
real proof is the upload round trip in §7.5.

**Neither body says anything but `ok` and `error` per check.** These
endpoints are reachable by anything that can reach the port, and `psycopg`
and `redis-py` both put the connection target into their error strings.
Diagnosis goes to the log; the probe gets a verdict.

### Observed behaviour, against real containers

Driven against the live dev Postgres and Redis on 2026-08-29:

| Situation | `/ready` | `/health` |
|---|---|---|
| both up | `200` `{"status":"ready","checks":{"database":"ok","redis":"ok"}}` in **0.04 s** | `200` in 0.002 s |
| Redis container stopped | `503` `{"database":"ok","redis":"error"}` in **0.10 s** | `200` in 0.015 s |
| Postgres container stopped | `503` `{"database":"error","redis":"ok"}` — **2.10 s** on the first probe, **4.08–4.10 s** steady state | `200` in **0.002–0.003 s** |
| both stopped | `503` `{"database":"error","redis":"error"}` | `200` |
| dependencies restarted | `200` again, **without restarting the app** | `200` |

Three things worth knowing from that:

- **4.1 s is not a timeout expiring.** The port is closed, so the connection
  is refused fast; `localhost` resolves to both `::1` and `127.0.0.1` and
  libpq pays ~2.0 s on each in turn. Against `127.0.0.1` alone the same probe
  answers in 2.04 s. **Budget `connect_timeout` × the number of addresses the
  hostname resolves to** — a managed endpoint with both an A and a AAAA
  record doubles it silently. Caddy's `health_timeout` is 10 s against that
  measured worst case rather than the 5 s that looks tidier.
- **The first probe after an outage begins is faster than the steady state**
  (2.10 s vs 4.10 s), because `pool_pre_ping` fails on an already-open pooled
  connection before a fresh connect is attempted. Do not tune a timeout
  against the first sample.
- **The worst case is the sum of both checks, because they run in series.**
  Observed once, when Docker Desktop's host port-forward blackholed Redis
  during container churn: **8.13 s**, with Redis's own 2 s connect timeout
  paid twice (both address families). Inside the 10 s budget, and the reason
  the budget is 10 s.

## 10. Rollback

```bash
git checkout <previous-release-tag>
sops exec-env infra/env/production.enc.env 'infra/deploy.sh'
```

**This rolls back code, not schema.** Alembic downgrades are not part of this
procedure and should not be improvised during an incident: a downgrade that
drops a column drops the PHI in it, and the note the doctor signed twenty
minutes ago is in that column.

If the release contained a migration the previous code cannot tolerate, the
rollback is a **PITR restore to the pre-migration timestamp step 4 printed**,
followed by a code rollback — and everything written between that timestamp
and now is gone. That is why step 3 prints the migration before step 4
applies it: the moment to notice is before, not after.

## 11. Routine operations

**Logs.** `docker compose -f infra/docker-compose.prod.yml logs -f api`.
Shipping and retention are Phase 5.2's.

**Restart one service.** `... restart worker`. Safe for `api`, `worker`,
`edge`. For `beat`, confirm exactly one comes back.

**Scale the worker** when uploads queue: `... up -d --scale worker=3`.
**Never scale beat** — see §7.4.

**Rotate `PHI_ENCRYPTION_KEY`.** [key-rotation](key-rotation.md). Note that
`get_settings()` is `@lru_cache`d, so secrets are read once at process start:
rotation is always a deploy, never a config poke.

**Host patching.** Unattended security upgrades on the VM, and a reboot
window. The client survives one (P0-2), which is the argument that made a
single VM defensible — it is not an argument for never rebooting.

## 12. Open, and owed to whoever runs this

- **A staging environment does not exist.** Every check in §7 would be run
  for the first time against the clinic's own deployment. The compose file is
  parameterised for one (`REMEDY_PUBLIC_DOMAIN`, per-environment sops
  documents); nothing else is.
- **No graceful drain.** On `SIGTERM` the API stops immediately rather than
  failing readiness first and letting Caddy drain it, so a deploy drops
  whatever was in flight. Caddy retries, so this is a rough edge and not an
  outage — but it is the next thing to fix, and it needs a lifespan hook in
  `app/main.py`.
- **Alerting on `/ready` going red is Phase 5.2's**, and until it exists
  nobody is told when Caddy takes the only instance out of rotation. A
  single-instance deployment whose readiness fails is an outage with no
  alarm.
- **The abort-incomplete-multipart lifecycle action has never been verified
  against anything** (decision 0014). §4 says how; it needs a real bucket.
