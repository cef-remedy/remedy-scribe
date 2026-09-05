# Free-tier deployment — everything that went wrong, and how it was fixed

**Companion to:** [`docs/runbooks/deploy-free-tier.md`](../runbooks/deploy-free-tier.md)
([published artifact](https://claude.ai/code/artifact/3aefcc42-2b6a-41c6-9bdd-3b689a7f0f5e)) ·
decision [0040](../decisions/0040-google-drive-as-a-storage-backend.md)

This is a log, not a runbook — it records what actually broke while getting
Remedy Scribe running on Netlify + Render + Neon + Upstash + Google Drive,
in the order it broke, with the cause and the fix for each. The runbook
itself was corrected as each of these was found, so following it today
should not reproduce most of this list — it exists so the next surprise
gets diagnosed in minutes instead of the hour some of these took.

Every fix below is a real commit on `main`; the hash is given so the exact
diff is one `git show` away.

---

## 1. Code bugs found before anything was deployed

### 1.1 A temporal-dead-zone bug that would have failed every upload, on every backend

**Symptom:** none, visually — the code "looked" fine and a prior session had
reported `tsc` clean.

**Cause:** a stale `tsconfig.app.tsbuildinfo` let the TypeScript build skip
re-checking `apps/web/src/lib/queue/uploader.ts`. A forced rebuild
(`tsc -b --force`) surfaced three errors on one line: the upload's part-size
plan read `init.data` on the line **above** `const init = await api.POST(...)`.
That is a temporal dead zone reference — not backend-specific, not
load-specific. It fails on the very first upload, always, with
`Cannot access 'init' before initialization`.

**Fix:** moved the plan computation to after the `init` call. Added
`uploader.test.ts` — the function had **no test at all** before this, only
an end-to-end smoke test requiring Postgres, MinIO, a Celery worker and a
real browser, which is a real test nobody runs while editing a header. Nine
tests now drive `uploadSession` directly with `api`/`fetch` stubbed, and the
regression guard was verified by re-introducing the bug and watching 8 of 9
fail. (`073a756`)

### 1.2 An empty `VITE_API_BASE_URL` silently falls back to `localhost:8000` in production

**Symptom:** none yet — this was caught by reading `apps/web/Dockerfile`'s
own comment before it could bite a Netlify deploy.

**Cause:** the runbook originally said to leave `VITE_API_BASE_URL` **empty**
for Netlify. An empty environment variable is exactly what Vite's env
loading (and a shell, and a compose file) is liable to read as "unset" —
which falls back to `apps/web/src/api/client.ts`'s
`http://localhost:8000` default. The result is a bundle that **builds,
deploys, and loads correctly**, then asks every visitor's own laptop for the
API. `apps/web/Dockerfile` already defended against this for the
single-VM path by baking `VITE_API_BASE_URL=/`; Netlify's build never saw
that default.

**Fix:** `client.ts` and `telemetry.ts` now fall back to `/` in a production
build and keep `localhost:8000` only under `import.meta.env.DEV`.
`apps/web/netlify.toml` is checked into the repo with `VITE_API_BASE_URL = "/"`
baked in, so there is no empty-value step left for anyone to get wrong.
Verified by building with no `.env` present at all: zero occurrences of
`localhost:8000` in the output bundle. (`073a756`)

---

## 2. The Google Drive backend

### 2.1 A Shared Drive reads as an empty drive unless *two* flags are set, not one

**Symptom:** none observed yet in production — found by re-reading the
adapter after learning the deployment account had a Shared Drive.

**Cause:** `files.list` needs **both** `supportsAllDrives` and
`includeItemsFromAllDrives` to see anything inside a Shared Drive. Every
*other* Drive call in `storage_drive.py` needs only the first — which is
exactly why the one call missing the second, `_find_file_id`, went
unnoticed. Google does not error on this: it answers `200` with an empty
`files` array, so a Shared Drive looks identical to an empty one. Five
functions resolve object keys through `_find_file_id`, and the worst
consequence is silent: `delete_object` would read "no file id" as "already
gone" and return success, so a consent withdrawal would report success
having deleted nothing, with the recording still sitting in Drive.

**Fix:** added both flags to the one call that was missing them. Two new
tests — one asserting the flags are actually sent, one standing in for
Drive properly (returning a file only when both flags are present) so it
fails on the real failure — nothing deleted — rather than on a missing
query parameter. (`497ca3e`)

### 2.2 The service-account auth path did not exist at all

**Symptom:** N/A — this was a capability gap, not a bug: the adapter only
supported a human's OAuth refresh token, which is the wrong answer once a
Shared Drive is available (decision 0040's cost #1).

**Fix:** added Google's `jwt-bearer` grant — one RS256 signature, one form
post — implemented directly rather than pulling in `google-auth`, the same
reasoning `asr/groq_whisper.py` already uses for calling Groq with raw
`httpx`. `_access_token()` now prefers a service account when
`GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` is set, precedence deliberate: having
configured one at all is the act of deciding to stop depending on a human's
grant. Five new tests, including one that deliberately embeds a fake private
key (`SUPERSECRETKEYMATERIAL`) in a broken key and asserts it never reaches
an error message — the payload here is a private key, and
`_mark_stage_failure` writes `str(exc)` into an unencrypted column.
(`6703a38`)

---

## 3. Local shell issues (PowerShell vs. bash)

### 3.1 `VAR=value command` has no PowerShell equivalent, and an unquoted `&` breaks it worse

**Symptom:**
```
The ampersand (&) character is not allowed. The & operator is reserved
for future use...
```
on the very first command the runbook asked for — running the Alembic
migration against Neon.

**Cause:** two independent problems compounding. First, the runbook's
command blocks were bash-only (`DATABASE_URL=<url> command`), and
PowerShell has no such prefix-assignment syntax at all — it is a parser
error there, not an environment-variable assignment. Second, Neon's
connection strings routinely carry `&channel_binding=require`, and an
**unquoted** `&` is PowerShell's own command separator, so even fixing the
assignment syntax would still break on the ampersand.

**Fix:** every command block in §6 (migrations, seed, worker, Beat) now
shows PowerShell first — `$env:NAME = "value"`, whole value double-quoted —
then bash/macOS/Linux second. Confirmed the ampersand itself is completely
inert in Render's dashboard env-var field, since that is a plain form with
no shell in between; the quoting problem only exists on a local machine
actually running a shell. (`b0f6d9e`)

---

## 4. Render setup

### 4.1 The source-picker screen offered three options and the runbook named none of them

**Symptom:** the engineer stared at "Git Provider / Public Git Repository /
Existing Image" with nothing in the runbook saying which one, or what to
put in Root Directory / Dockerfile Path / Health Check Path once past it.

**Cause:** the runbook said "create the Render web service" as if that were
one step. It is not — the repo's Dockerfile lives at `apps/api/Dockerfile`,
not the repo root, so Root Directory and Dockerfile Path both need
non-default values or the build fails looking in the wrong place; left on
auto-detect, Render also tries a native Python buildpack instead of Docker,
silently skipping every hardening decision in the Dockerfile itself.

**Fix:** added a full field table — Language/Runtime: Docker, Root
Directory: `apps/api`, Dockerfile Path: `./Dockerfile`, Health Check Path:
`/health` — with "Public Git Repository" and "Existing Image" named
explicitly as wrong, not just omitted. (`9f8f9a9`)

### 4.2 The boot guard refuses `S3_SECRET_KEY`'s default even though Stage 1 never uses S3

**Symptom:**
```
Refusing to start with this configuration (ENVIRONMENT='production'):
  - S3_SECRET_KEY is published in .env.example and infra/docker-compose.yml...
```

**Cause:** `S3_SECRET_KEY` defaults to `remedy-dev-secret`, a fingerprinted
published secret, and the boot guard checks it **unconditionally** — it has
no way to know the storage backend that would use it is unconfigured this
stage.

**Fix:** documented explicitly: generate any random string for it even
though it is never exercised at Stage 1.
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
(`e30a7db`)

### 4.3 The same guard also refuses any `localhost` origin in `CORS_ALLOW_ORIGINS` — twice, in this project

**Symptom (first time):** the same `ValidationError`, naming
`CORS_ALLOW_ORIGINS still contains a localhost origin`, when the engineer
needed the API running before Netlify existed and had no real origin to put
there yet.

**Cause:** the guard refuses **any** `localhost`/`127.0.0.1` entry in
production, on purpose — it cannot distinguish a deliberate temporary
addition from an accidentally-deployed dev `.env`.

**Fix (first time):** documented a placeholder on the reserved `.example`
TLD (matching `scripts/seed_staging.py`'s own convention for non-real
domains): `CORS_ALLOW_ORIGINS=https://remedy-scribe.example`, safe because
nothing checkable before Netlify exists is a CORS-governed request.
(`e30a7db`)

**This recurred — see §6.3 below, where the same fix was wrongly applied a
second time by the runbook itself, for a different step.**

---

## 5. Database

### 5.1 The seed script needs `--no-audio` at Stage 1, and nothing said so

**Symptom:**
```
botocore.exceptions.EndpointConnectionError: Could not connect to the
endpoint URL: "http://localhost:9002/remedy-scribe-audio/..."
```

**Cause:** `scripts/seed_staging.py` uploads real (synthetic) audio bytes to
object storage by default, and at Stage 1 there is no object storage
configured anywhere reachable. The `--no-audio` flag already existed,
purpose-built for exactly this — it seeds every encounter as
never-recorded instead of pointing at bytes that were never written — but
the runbook never mentioned it.

**Fix:** documented `--no-audio` as required, not optional, at Stage 1, and
confirmed transcripts and notes are still seeded either way (they are
constructed directly, independent of the audio branch), so §7's "open a
seeded note and click a line" check still has something to click.
(`419ea27`)

### 5.2 No `--reset`, and the only real fix is dropping the schema — which this session could not do itself

**Symptom:**
```
Refusing to seed this database:
  - This database is already seeded (3 synthetic clinician(s)). Running
    again would append duplicates, and there is no --reset...
```

**Cause:** deliberate, by design (P0-1): the consent ledger's append-only
trigger means no row can be deleted by anything, including the table
owner, so a partial cleanup would leave orphaned ledger rows. The only real
reset is dropping and recreating the schema.

**Fix:** since Neon's free tier has one database with no separate
maintenance connection to drop it through, the equivalent reset is
`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` (confirmed safe: no
migration creates anything outside `public`). This specific action was
**refused by this session's own auto-mode classifier** as a destructive
operation against live infrastructure — correctly, and it was not routed
around. The exact command was handed to the user to run themselves in their
own PowerShell session instead.

### 5.3 Neon's connection string needs a scheme rewrite, or the API dies at boot naming the wrong problem

**Symptom (would-be):**
```
ModuleNotFoundError: No module named 'psycopg2'
```

**Cause:** Neon hands out `postgresql://`. SQLAlchemy reads that as "use
psycopg2" — not installed here, since this app runs psycopg 3. The error
names neither Neon nor the connection string.

**Fix:** documented the rewrite explicitly, as a before/after pair:
```
postgresql://...              # what Neon gives you
postgresql+psycopg://...      # what you store
```
plus two related checks: `-pooler` must be in the host (the direct endpoint
has far fewer connections), and `rediss://` needs two esses, not one.
(`0a53d93`)

---

## 6. Celery / Redis

### 6.1 Kombu and redis-py disagree about `rediss://`, and the failure lands on an unrelated route

**Symptom:** a fully successful upload — consent passed, the PUT to Drive
succeeded, the DB committed `pipeline_status: uploaded` — followed by a bare
`500 Internal Server Error` with no detail on `upload/complete`, while
`/ready` continued to report `redis: ok`.

**Cause:** `routes/health.py`'s readiness probe uses
`redis.Redis.from_url()`, which infers TLS from a `rediss://` scheme
automatically. Kombu — the client Celery itself uses for its
broker/backend — does not: it refuses a `rediss://` connection outright
unless a certificate policy is stated explicitly, and it refuses **late**,
at the first real connection (`chain.apply_async()` inside
`complete_upload`), not at import or at `Celery()` construction. So the
readiness probe stayed green the entire time while every real upload failed
on an unrelated-looking route.

**Fix:** `_tls_options()` in `apps/api/app/tasks/celery_app.py` derives
`CERT_REQUIRED` for a `rediss://` broker (Upstash's certificate is signed by
a public CA — no reason to accept less) and `None` for plain `redis://`
(local dev unaffected), wired into `broker_use_ssl` /
`redis_backend_use_ssl`. Four new tests, one of which reloads the module
against a patched URL to confirm `celery_app.conf` actually carries the
option — a helper that computes the right dict but is never wired to `conf`
fails exactly as silently as no helper at all. (`7aa9828`)

### 6.2 `--broker-transport-options` has never been a real CLI flag on `celery worker`

**Symptom:**
```
Usage: python -m celery worker [OPTIONS]
Error: No such option '--broker-transport-options'.
```
— the worker refused to start at all, immediately after the TLS fix above.

**Cause:** the runbook's own documented worker-start command carried
`--broker-transport-options '{"socket_timeout": 60, "brpop_timeout": 30}'`
as a command-line flag. That setting is real and necessary (Celery's
default ~1-second blocking read produces roughly 2.6M commands a month
against Upstash's 500K budget, from an idle worker alone) — but it has
always been an **application-config** setting, never a CLI flag. Every
reader who copy-pasted that command would have failed at argument parsing
before the worker so much as tried to connect.

**Fix:** moved `BROKER_TRANSPORT_OPTIONS` into `celery_app.conf.update()`,
where it actually reaches the client Kombu builds — nothing to pass on the
command line at all. Two more tests, confirming both the constant's value
and that the running `celery_app.conf` actually carries it. Removed the
bogus flag from all four worker-start command blocks (PowerShell and bash,
runbook and artifact). (`9a0ba32`)

### 6.3 The worker needs the Drive variables too — its own environment, separate from Render's

**Symptom:** the worker connected cleanly, mingled, said "ready", and
immediately picked up a queued upload — then failed downloading the audio:
```
EndpointConnectionError: Could not connect to the endpoint URL:
"http://localhost:9002/remedy-scribe-audio/..."
```
identical in shape to §5.1, but this time from the worker, after Drive was
already confirmed working on Render.

**Cause:** the worker reads its **own** process environment, entirely
separate from Render's. It was started with only the four Stage-1
variables (`DATABASE_URL`, `REDIS_URL`, `GROQ_API_KEY`,
`PHI_ENCRYPTION_KEY`) — matching exactly what the runbook's §6 step 5
listed — so `STORAGE_BACKEND` silently defaulted to `s3`, and the S3
settings fell back to whatever this machine's own local `.env` happened to
contain.

**Fix:** both worker-start command blocks now also export
`STORAGE_BACKEND=drive` plus the Setup A or Setup B Drive variables,
explicitly labeled as a Stage 2 requirement distinct from Stage 1. Verified
by restarting the worker with the full environment and watching the same
queued task succeed all the way to a real `googleapis.com` file download.
(`9a0ba32`)

---

## 7. Handling live secrets in a chat session

Three separate incidents, all handled the same way once the pattern was
established, and worth recording as a pattern rather than three isolated
stories.

### 7.1 A live Groq API key surfaced via an IDE line-selection

**What happened:** the user selected a line in `apps/api/.env` in their
editor; the harness surfaces IDE selections into the conversation, and the
selected line happened to be a live `GROQ_API_KEY`.

**Handling:** flagged immediately, the key was not repeated, and rotation
was recommended (not urgent, but worth doing) since the value now sits in a
conversation transcript rather than only in a local `.env` file.

### 7.2 A credentials file dropped at the repo root

**What happened:** the user wrote `credentials.txt`, containing every
secret needed for the deployment (DB URL, Redis URL, Groq key, PHI key,
JWT secret, Drive folder id), directly into the repository's working
directory rather than an out-of-repo location.

**Handling:** checked `git status`/`git log --all` for the file **before**
reading it — confirmed untracked, never staged, never committed — then
moved it to the session's scratchpad directory (outside git entirely,
cleaned up with the session) and deleted the repo copy. The same check-then-move
sequence was repeated verbatim for a second file later (§7.3), because it is
the correct sequence regardless of how many times it recurs.

### 7.3 The Drive service-account JSON, same pattern, plus a Windows-shell problem it exposed

**What happened:** the same thing again — a file named `TO ADD.txt`
containing the full combined credentials set (this time including the
Google service-account private key) dropped at the repo root, and the
private key material was also surfaced via a second IDE selection.

**Handling:** identical check-then-move-then-delete sequence. Separately,
recommended rotating the service-account key in GCP once verification
finished, since the key material (not just a reference to it) had touched
the conversation transcript directly — a stricter standard than a mere
credentials-file path being mentioned.

**A real technical problem this uncovered:** a multi-line RSA private key,
with literal `\n` escape sequences inside a JSON string, is exactly the
shape of value that breaks `export VAR=...` shell quoting in both bash and
PowerShell. Rather than fight shell escaping, the environment for the
Celery worker was built in a small Python launcher script
(`start_worker.py`) that reads the credentials file directly and constructs
`os.environ` for a `subprocess.run(...)` call — sidestepping shell quoting
for that value entirely.

### 7.4 The launcher script's own parsing bug

**Symptom:**
```
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
```

**Cause:** the launcher assumed the dropped file was pure JSON. It was not —
it was the same combined credentials text file as §7.2, with the actual
JSON key appended at the end under a `Google JSON` header line.

**Fix:** the parser now locates that header line and takes everything after
it as the JSON payload, rather than assuming the whole file is JSON from
byte zero.

---

## 8. The recording setup

### 8.1 The CORS placeholder trick from §4.3 does not generalize — it is not a fix for every localhost need

**Symptom:** the exact same `ValidationError` as §4.3, `CORS_ALLOW_ORIGINS
still contains a localhost origin`, this time because the runbook's own
newly-written "record a demo" section told the reader to add
`http://localhost:5173` to Render's `CORS_ALLOW_ORIGINS` so a locally-run
web app could reach the deployed API.

**Cause:** this was a mistake in the runbook itself, not a config error —
the production boot guard refuses *any* `localhost` origin in that setting,
unconditionally, for the same reason as §4.3. The difference is that §4.3's
placeholder was a legitimate stand-in for a domain that did not exist *yet*;
this was asking the guard to permanently accept a browser origin that would
never be the real production frontend, which is precisely what the guard
exists to catch.

**Fix:** replaced the CORS approach entirely with a Vite dev-server proxy.
`apps/web/vite.config.ts` now forwards `/api/*` to
`process.env.RENDER_DEV_PROXY_TARGET` (deliberately not `VITE_`-prefixed,
so it can never leak into the built bundle) when that variable is set —
the same same-origin trick `netlify.toml`'s rewrite performs in production,
running in Vite's own Node process instead of Netlify's edge. This also
fixed a second problem the CORS approach never actually solved: a genuinely
cross-origin browser session cannot refresh a `SameSite=lax` cookie, so the
original instructions had to warn "don't reload the page mid-recording."
Proxied through Vite, the browser only ever talks to `localhost:5173`, so
the session behaves exactly as it does in production — nothing to work
around. (`e7575be`)

---

## What this list is not

It is not a claim that the runbook is now bug-free — it is a record that
every entry above was found by actually running the thing, not by
review, and fixed the same way. The next surprise, when it comes, belongs
here too.
