# 0036 — One VM, and exactly one bought database

**Phase:** 5.1 · **Decided by:** implementation (architecture), user/Legal (jurisdiction) · **Date:** 2026-08-28

## The half of this 🧠 that is not mine, said first

The checklist asks two questions and they are not the same question.

**"In which jurisdiction?" is Legal's, and it is genuinely blocking.** Data
residency for Philippine health data may eliminate providers before any
technical criterion is applied, and every recommendation below that names a
*vendor* is therefore conditional. This decision does not pick one, and it
should not be read as having picked one by implication. What is written
here is a **shape** — one VM, one managed database, one edge — that can be
instantiated on any provider that clears the residency bar.

What I need from Legal to finish the choice, in the order it eliminates
options:

1. **Must PHI at rest stay physically in the Philippines?** This is the
   question. If yes, the hyperscalers' Singapore and Hong Kong regions are
   out, which removes most of the managed-Postgres market and makes a
   local provider or self-hosting the only path — and self-hosting
   Postgres changes the PITR argument in §2 materially.
2. **Does that also bind the *processor* boundary?** Groq already holds the
   audio and now the transcript (decisions 0018, 0035), and Groq's regions
   are not Philippine. If PHI may leave the country for processing but not
   for storage, the answer differs from "not at all" — and if it is "not at
   all", the vendor decision of Phase 4 has a problem this phase cannot
   solve.
3. **Does key material follow the same rule as the data it protects?**
   §3's secret-manager choice turns on this. `PHI_ENCRYPTION_KEY` is not
   PHI, but it is the only thing standing between a database copy and
   plaintext.
4. **Is the pilot's DPA/BAA with the hosting provider signed before, or
   after, the first real recording?** This sets whether the pilot can start
   on a staging-grade arrangement.

Two things worth saying to Legal in the same conversation, because they
change the shape of the question: **there is still no designated DPO and no
breach response team** (Phase 4.3 shipped that roles table empty on
purpose, and both are legally required), and **the breach runbook's legal
section has never been reviewed by counsel** — NPC Circular 16-03 was not
read first-hand because `privacy.gov.ph` returns 403 to direct fetching.

Everything below is decidable without that answer, and is decided.

## 1. One Linux VM running Docker Compose

The checklist's own principle is *"the least infrastructure that meets the
compliance bar,"* and the honest reading of this pilot's scale is: one
clinic, a handful of doctors, laptops (decision 0024). The load is a few
concurrent consultations. The expensive work — ASR and note generation — is
someone else's compute behind an HTTP call.

Options considered:

- **Kubernetes.** Rejected, and the checklist says so first. The compliance
  bar here is about *where data lives and who can read it*, and a control
  plane does not move that bar. What it adds is a second system to secure,
  patch and understand, at a scale where the workload is five containers.
- **Managed containers** (ECS/Fargate, Cloud Run, App Service). A real
  option, and the checklist's own summary of it is accurate: more money,
  fewer 2 a.m. problems. Rejected *for now* on two grounds. First, Cloud
  Run-style request-scoped containers fit the API and fit Celery beat
  badly — beat is a long-lived singleton and §4 explains why exactly one of
  it is a PHI-safety property, not a preference. Second, it forecloses the
  residency question in the other direction: managed container platforms
  exist only where their provider has a region.
- **A single VM with Docker Compose.** Chosen. Five containers, one
  `docker compose up`, one host to patch, one firewall to write. Every
  operational procedure in `docs/runbooks/` is already written against
  `docker exec` and `docker compose`.

The cost is stated rather than glossed: **this topology has no redundancy.**
The VM is a single point of failure and a reboot is an outage. That is
acceptable here specifically because P0-2 built the client to work through
one — a doctor keeps recording into IndexedDB with the network gone, and the
upload queue drains when it returns. The system degrades to "notes are
delayed", not "the consultation is lost". A topology whose outage story
depended on the server being up would not be defensible at one replica; this
one's does not.

## 2. Postgres is the one thing bought, and PITR is the reason

Everything else on this list is run on the VM. Postgres is not.

The checklist asks for "automated backups and PITR", and those two are not
the same difficulty. `pg_dump` on a cron is an afternoon's work and
`docs/runbooks/backup-restore.md` already documents it, drill and all.
Point-in-time recovery is continuous WAL archiving, a retention policy for
the archive, and — the part that is actually hard — a *restore path that has
been exercised*, because an untested WAL archive is a hope (decision 0034,
and the backup runbook's own §6 says the tested RPO today is "whenever the
last dump ran").

Two things make that gap unacceptable rather than merely untidy:

- **A day of lost consultations is not an acceptable RPO for a clinical
  record.** The backup runbook says this in the same words and marks it
  **[5.1]**.
- **Under the Data Privacy Act, unrecoverable loss of patient records is a
  reportable availability incident** — not an outage, an incident. That is
  already written down in the key-rotation runbook's failure section for the
  key; it is equally true of the database.

So Postgres is the one component where "operate it ourselves" costs more
than it saves, and it is bought. Everything else stays on the VM. Buying
*more* than this would be buying convenience; buying this is buying a
recovery point.

The connection is `sslmode=verify-full`, not `require` — `require` encrypts
without authenticating, which stops a passive eavesdropper and not an active
one. The PHI columns are Fernet ciphertext, but birthdates, clinician
emails, the whole audit log and the entire consent ledger are not, and they
travel on that connection. That is the "TLS to Postgres" leg Phase 4.1's
runbook owed to this phase.

## 3. Redis is self-hosted, and here is the data-loss window in numbers

5.1 offers "managed Redis, or accept and document the data-loss window."
This accepts it. The window is worth writing out, because the reason it is
acceptable is not "it's only a queue."

**What Redis holds here.** The Celery broker and result backend, and nothing
else. No PHI cache. No sessions — the refresh token is an httpOnly cookie
plus a database row. No rate-limit counters: decision 0008 deliberately put
login rate limiting in Postgres precisely so that it survives a broker
restart. Task arguments are encounter *ids*: `run_pipeline(encounter_id)`,
`transcribe_encounter(encounter_id)`. **No PHI passes through Redis at all**,
which also means losing it is not a disclosure question.

**What is lost.** Enqueued-but-unstarted task messages — with
`appendonly yes` / `appendfsync everysec`, up to about one second of them.
Clinically: a consultation whose audio finished uploading and whose
transcription had not yet been picked up.

**Why that is acceptable.** *The compensating control already exists, and it
was built for this exact case by someone who was not thinking about Redis
hosting.* `sweep-stuck-encounters` runs every 300 s and re-kicks any
encounter left in a non-terminal `pipeline_status` past
`PIPELINE_STUCK_THRESHOLD_MINUTES` (30 by default). Its own comment in
`app/tasks/celery_app.py` says it exists to catch "the task that never ran
at all — the broker down, or the worker pool at zero, at the moment
`run_pipeline` fired." A lost enqueue is therefore a **delayed** encounter,
bounded at 35 minutes, not a lost one. The audio is already in object
storage and the encounter row already points at it.

Managed Redis would shorten that delay. It would not remove the failure
mode — a managed instance failing over also drops unpersisted state — and it
would add a vendor to name in the Data Privacy Act processor disclosure and
a fourth residency question for Legal, in exchange for not patching one
container.

**This argument is scoped to Redis living on the same host.** `REDIS_URL` is
`redis://` for exactly that reason: there is no network segment between the
app and the broker for TLS to protect. The moment Redis moves off this VM,
the scheme becomes `rediss://` and this whole section needs re-reading.

## 4. Exactly one Beat process, enforced by a name

`app/tasks/celery_app.py` now declares two Beat schedules:
`sweep-stuck-encounters` every 300 s and, since Phase 4.4,
`sweep-expired-retention` hourly. The second one **deletes PHI** —
transcripts, note revisions, audio objects past their retention clock, plus
the backstop delete for a patient's withdrawal (decision 0033).

Celery's default scheduler holds no distributed lock. Two beat processes are
not deduplicated; they are two concurrent passes over the same deletion
candidates, racing each other, against rows that are gone afterwards.
`sweep-stuck-encounters` firing twice is wasteful. The retention purge
firing twice is unrecoverable.

Options considered:

- **Write "do not scale beat" in the runbook.** This is what most projects
  do, and it holds until the first incident where someone scales everything.
- **celery-redbeat**, which takes a Redis lock. Correct, and the right
  answer if beat ever needs to be genuinely redundant. Today it adds a
  dependency to make a singleton safe to duplicate, which is not a problem
  this deployment has.
- **Chosen: a fixed `container_name` on the beat service.** Docker will not
  create two containers with one name, so Compose refuses to scale a service
  that has one. Verified rather than assumed: `--scale beat=2` warns and
  starts exactly one, while `--scale worker=3` starts three. It is not a
  hard error — but the property that matters is that there is no way to get
  two, and it costs one line.

Scaling `worker` remains free and is the right knob if uploads queue.

## 5. The web app is served by the same origin as the API

There is no web Dockerfile today; the app is a static Vite build. The
options are a static host (Netlify, S3+CDN) on its own hostname, or the same
edge that terminates TLS for the API. Chosen: the same edge, one origin —
`apps/web/Dockerfile` builds the assets and bakes them into a Caddy image.

Four reasons, and the first is a security property rather than a
convenience:

1. **The refresh cookie.** Decision 0024 put the refresh token in an
   httpOnly cookie. Split origins make it cross-site, which forces
   `SameSite=None` — strictly weaker, sent on every cross-site request.
   `app/core/config.py`'s own comment says "paired with samesite=none only
   if the API and client are cross-site"; this topology is what keeps `lax`
   correct.
2. **CORS stops being load-bearing.** Same-origin requests are not
   preflighted at all. `main.py`'s comment calls a CORS misconfiguration a
   *silent* failure — rejected preflight, nothing in the API log. This
   removes the failure rather than documenting it.
3. **The service worker.** `vite.config.ts` already sets
   `navigateFallbackDenylist: [/^\/api\//]`. That rule is only meaningful if
   `/api/*` shares the service worker's origin; on split origins the SW
   could never intercept API calls and the denylist would be decorative. The
   existing PWA config already assumes this topology.
4. One certificate, one TLS policy, and exactly one place that sets
   `X-Forwarded-Proto` and `X-Forwarded-For`.

The cost: no CDN. For one clinic, the service worker precaches the assets
after first load (P0-2), so the second visit fetches none of them regardless
of where they are served from — a CDN would optimise a request that does not
happen.

**Caddy rather than nginx**, because automatic ACME certificates with
automatic renewal, the http→https redirect, a TLS 1.2 floor and a modern
cipher list are its *defaults*. Those are four of the five items
`docs/runbooks/key-rotation.md` §"What Phase 5 still owes" lists. With nginx
each is explicit config plus a certbot timer nobody notices has stopped
until a certificate expires.

### The X-Forwarded-For finding, which was not on anyone's list

4.1's runbook flags `X-Forwarded-Proto` as the header that must be set by
the proxy and by nobody else. Reading the code for that turned up a second
one that nothing had flagged.

`app/api/routes/auth.py:_client_ip` returns `request.client.host`, and that
value keys the per-IP login rate limit and the lockout counter. Behind a
reverse proxy, every request appears to come from the proxy — so
`LOGIN_RATE_LIMIT_PER_IP_PER_MINUTE = 10` silently becomes *ten login
attempts per minute for the entire clinic*. Two doctors mistyping a password
in the same minute is a self-inflicted outage.

The fix is uvicorn's `--proxy-headers`, and the interesting part is the
argument it takes. Reading uvicorn 0.30.6's `ProxyHeadersMiddleware` rather
than trusting the flag: with `--forwarded-allow-ips=*` it returns
`x_forwarded_for_hosts[0]` — the **leftmost**, entirely client-supplied
entry. A wildcard would therefore hand an attacker a fresh apparent IP per
login attempt and delete the rate limiter. With a *specific* trusted address
it instead scans from the right for the first untrusted hop, which is
correct. So the edge container gets a static IP on a fixed subnet and that
literal address is what uvicorn trusts — uvicorn does not accept CIDR at
this version.

Belt and braces: Caddy is also told `header_up X-Forwarded-For {remote_host}`,
which **replaces** the header with the address it actually accepted the
connection from, so there is no client-supplied entry left to reason about
under either ordering.

## 6. Migrations are a service with a profile, not a rule people remember

`alembic upgrade head` is not run on boot anywhere today, and the checklist
says keep it that way. The way to keep it that way is to make the wrong
thing impossible rather than discouraged.

`migrate` is a service in the production compose file carrying
`profiles: ["deploy"]`. Compose skips profiled services on a plain
`docker compose up -d` — which is the command someone will actually type at
2 a.m. — so the migration cannot ride along with a restart. It exists only
when `--profile deploy` names it, which `infra/deploy.sh` does once, as its
own step, whose exit code gates whether the long-running services are
started at all. Verified: `config --services` lists five services; the same
command with `--profile deploy` lists six.

The failure this prevents is specific. Three long-lived services start from
the same image. A migration in an entrypoint means three processes racing
for the same Alembic lock on every restart, and a container that OOM-restarts
at 3 a.m. quietly applying a schema change nobody triggered.

## 7. Liveness and readiness are different questions with opposite answers

`/health` was defined inline in `main.py` and checked nothing but its own
existence. The checklist calls that out. The fix is not "add a database
check to `/health`" — that is the trap, and it is worth stating why, because
the wrong version looks more thorough.

**Liveness failing restarts the container. Readiness failing removes it from
traffic.** A liveness probe that touches Postgres restarts a perfectly
healthy application because the database blinked — and it restarts every
replica at once, because they all share the dependency. A 30-second
dependency outage becomes a crash loop that outlives its cause. So:

- **`GET /health`** — liveness. Checks nothing external, on purpose. Its
  payload is unchanged from the inline version, because a probe path that
  shifts under a refactor un-monitors a deployment quietly.
- **`GET /ready`** — readiness. `SELECT 1` through the *application's own
  pooled session* (a healthy Postgres reached through an exhausted pool is
  an instance that cannot serve traffic, and a probe opening its own
  connection would report ready throughout), and a Redis `PING` with a
  bounded timeout. 503 on either.

**Object storage is deliberately excluded from readiness.** Three reasons.
It would convert a partial outage into a total one — with the bucket down,
uploads and playback fail but consent capture, worklists, patient matching,
review and signing all still work, and P0-2's offline queue exists so a
doctor keeps recording through it; pulling every instance from the load
balancer takes those down too. The check is also slow and remote: a
`HeadBucket` wrapped in botocore's retry and backoff, paid every ten
seconds, risks the probe exceeding its own timeout and causing that same
total outage *by accident*. And it proves little — what matters is whether a
presigned multipart round trip works, which the deploy's own smoke step
verifies once, with stronger evidence.

**Neither body says anything but `ok` and `error` per check.** These
endpoints are unauthenticated by necessity — an orchestrator has no
credential — and `psycopg` and `redis-py` both put the connection target
into their error strings, so forwarding an exception message would publish
`DATABASE_URL` to anything that can reach the port. Phase 4 already found
the sharper version of this: `_extract_tool_input` interpolated a whole
model response, PHI included, into an exception message that was then
written to a plain column. The diagnosis goes to the log; the probe gets a
verdict.

## What was measured rather than assumed

Two numbers, both from stopping a real container against a real app:

- **A readiness probe with Postgres down answers in 4.1 s** — and that is
  not a timeout expiring. The port is closed, so the connection is refused
  fast; `localhost` resolves to both `::1` and `127.0.0.1` and libpq pays
  ~2.0 s on each in turn. Against `127.0.0.1` alone the same probe answers
  in 2.04 s. **The probe budget is therefore `connect_timeout` × the number
  of addresses the hostname resolves to**, which a managed endpoint with
  both an A and a AAAA record doubles silently. Caddy's `health_timeout` is
  10 s against that measured 4.1 s rather than the 5 s that looks tidier.
- **`connect_timeout` did not shorten any of it** (libpq floors it at 2 s,
  and a refusal was already faster than the floor). It is in the URL anyway,
  because the case it bounds is the one a stopped container cannot
  reproduce: a host that accepts the SYN and never completes the handshake.
  Unbounded, that is minutes.

And one defect found by building the image rather than reading the
Dockerfile: **`COPY . .` was baking `apps/api/.env` into the image.** Built
without a `.dockerignore`, the image contains `/app/.env` carrying a working
`PHI_ENCRYPTION_KEY` — and on this machine that key is *not* one of the
published dev secrets, so Phase 4.1's boot guard would not have caught it. A
layer is immutable: removing the value from the environment later does not
remove it from the image. The secrets runbook's rule that no `.env` reaches
a server was being defeated before the deploy started.

## What would change my mind

- **Legal answering "PHI must stay in the Philippines."** §2's managed
  Postgres may then not exist at an acceptable quality, and self-hosted
  Postgres with WAL archiving changes the calculus in §1 too — it is the one
  thing on the VM whose failure is unrecoverable, and running it there
  brings back exactly the operational burden this shape was chosen to avoid.
- **A second clinic.** One VM is defensible for a pilot in Remedy's own
  clinics and stops being so the moment an outage affects a site that cannot
  call the person who runs the server. Two clinics is the trigger to revisit
  managed containers — not more traffic.
- **The sweeper's 35-minute worst case becoming clinically visible.** If a
  doctor ever waits half an hour for a note because Redis restarted,
  §3's argument has failed in practice and managed Redis is worth its price.
- **Beat needing to be redundant.** Then celery-redbeat, and §4's
  `container_name` guard comes out — but the guard should not come out
  first.
- **Object storage outages turning out to be correlated with, rather than
  independent of, the rest.** §7's exclusion assumes a bucket can be down
  while the database is fine. If the chosen provider makes those the same
  failure, the readiness check has one dependency fewer to argue about.
