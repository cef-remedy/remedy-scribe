# Running Remedy Scribe locally

Two audiences: **you**, wanting to click through what has been built, and
**another developer** who has just cloned the repo and has nothing set up.
Both start at Path A.

Nothing here is deployed anywhere. Everything below runs on one machine.

---

## Path A — see it working in ~10 minutes (no API keys needed)

This seeds a realistic database and signs you in. You get the worklist,
patient search, the review screen, grounding highlights, signing, and the
pilot report — **everything except making a brand-new recording**, which
needs a vendor key (Path B).

### 1. Prerequisites

| | |
|---|---|
| Python 3.13 | `python --version` |
| Node 20+ | `node --version` |
| Docker Desktop | **must be running** — `docker ps` should not error |
| A TOTP app | 1Password, Authy, Google Authenticator. Login requires MFA. |

### 2. Backend setup

```bash
cd apps/api
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # Windows
# macOS/Linux: .venv/bin/pip install -r requirements-dev.txt

cp .env.example .env
```

Generate a PHI encryption key and paste it into `.env` as
`PHI_ENCRYPTION_KEY`:

```bash
.venv/Scripts/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> **Why this matters:** every patient name, note section and transcript is
> encrypted with this key. Lose it and that data is unrecoverable — there is
> no recovery path, by design. For local dev that is fine; just don't expect
> a database seeded under one key to be readable under another. See
> `docs/runbooks/key-rotation.md`.

### 3. Start the infrastructure

```bash
docker compose -f ../../infra/docker-compose.yml up -d postgres redis minio
```

> **Non-standard ports are deliberate:** Postgres is on **5433**, Redis on
> **6380**, MinIO on **9002/9003**. The machine this was built on had native
> Postgres and Redis squatting on the standard ports and silently swallowing
> Docker's forwarded traffic. `.env.example` already matches these.

### 4. Migrate and seed

```bash
.venv/Scripts/python -m alembic upgrade head

REMEDY_ALLOW_SYNTHETIC_SEED=1 .venv/Scripts/python scripts/seed_staging.py --yes
```

The seed refuses to run against anything that looks like production — six
independent locks, including one that trusts no config at all (every
clinician row must be on an RFC 2606 reserved domain). It prints its
sign-in credentials at the end, including a live TOTP code.

It ends with a **grounding read-back**: it re-reads every note it created
through the real resolution path and confirms the citations resolve. If that
fails, stop — the dataset is not trustworthy.

### 5. Run the API and the client

Two terminals:

```bash
# terminal 1 — from apps/api
.venv/Scripts/python -m uvicorn app.main:app --reload

# terminal 2 — from apps/web
npm install
npm run dev
```

Check the API is genuinely up, not just listening:

```bash
curl http://localhost:8000/health    # the process answers
curl http://localhost:8000/ready     # ...and can reach Postgres and Redis
```

`/ready` returning **503** with `{"database":"error"}` means the containers
aren't up. That distinction is deliberate: `/health` never touches the
database, so a database blip can't trigger a container restart loop.

### 6. Sign in

Open **http://localhost:5173**.

- **Email:** `doctor@staging.remedy.example`
- **Password:** `staging-not-a-real-password`
- **MFA code:** from your TOTP app, using the secret the seed printed —
  or generate one directly:

```bash
cd apps/api
.venv/Scripts/python -c "import pyotp; print(pyotp.TOTP('REMEDYSTAGINGSEEDMFA2222').now())"
```

Codes expire every 30 seconds. A rejected login is far more often a stale
code than a wrong password.

### 7. What to actually look at

The seeded data was built to exercise the interesting states, not to look
tidy. Worth clicking:

| What | Where | What you should see |
|---|---|---|
| **Grounding** | Open a note → click any line | The transcript passage it came from, with a timestamp. Click again to play that moment. |
| **A line citing nothing** | Same screen | A wavy red underline. That is the line worth scrutinising — the model wrote it without pointing at anything. |
| **Withheld grounding** | The note with an edited section | "Source links no longer line up" instead of a highlight. The offsets stopped matching after an edit, so it refuses to guess. |
| **The degradation ladder** | The consent-withdrawn encounter | "The patient withdrew consent…" — not "retention expired". Different reason, different words. |
| **Patient search** | Home → link a loose session | Type `Jun Aquino` or a misspelling. Fuzzy matching over encrypted names. |
| **The signing ceremony** | A note at `authenticated` | Visually separate, needs the PRC licence typed. After signing, the note is immutable and the star-rating prompt appears. |
| **The pilot report** | `curl` below | Edit burden, documentation time, ratings — no PHI in it by design. |

```bash
# grab a token, then read the report
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"doctor@staging.remedy.example","password":"staging-not-a-real-password","mfa_code":"REPLACE"}'
```

Also worth doing: sign in as `compliance@staging.remedy.example` (same
password and MFA) and try to open a note — you'll see RBAC refuse a write
while permitting the read, and the access appear in the audit log.

---

## Path B — record a real consultation end to end

Path A cannot make a **new** recording, because transcription and note
generation are real vendor calls. Without a key, an upload reaches storage
and then stops at `transcription_failed` after three retries.

### Get a Groq key

1. Sign up at <https://console.groq.com> and create an API key.
2. Put it in `apps/api/.env` as `GROQ_API_KEY=…`.

⚠️ **Two things to know before using this with anything real.** Groq's free
tier allows roughly **8,000 tokens per minute**, and a 20–40 minute
consultation transcript is 10,000–20,000 tokens sent in one call — so a
*single real consultation* exceeds it. Short test recordings are fine; a
clinic day is not. And Groq's BAA **excludes free-tier usage**, so the free
tier is precisely the tier with no data-protection undertaking. Fine for
testing with your own voice. Not fine for a patient. See decision 0035.

### Run the worker

Transcription and note generation run in Celery, not in the API process.
Nothing happens without this:

```bash
cd apps/api
.venv/Scripts/python -m celery -A app.tasks.celery_app worker --loglevel=info --pool=solo
```

`--pool=solo` is needed on Windows; the default pool doesn't work there.

### Then

1. Home → **New encounter**
2. Read the consent script and record consent (bilingual; the script text is
   still a placeholder awaiting counsel — see the RA 4200 note below)
3. Record for 15–30 seconds, saying something clinical
4. Stop. Watch the worker log: `transcribe_encounter` → `generate_note`
5. The worklist shows a note id when it's done — open it

If it stalls at `uploaded`, the worker isn't running or the key is missing.
The encounter row's `last_pipeline_error` will say which.

### No key at all?

There's a substitution used by the end-to-end tests that stands in for the
two vendor calls while keeping everything else real — the recording, the
upload to MinIO, presigned playback, span resolution and the whole UI:

```bash
cd apps/web
SEED_PIPELINE=1 MFA_SECRET=<secret> PW_PATH=<path-to-playwright> node smoke/grounding-flow.cjs
```

---

## Running the tests

```bash
cd apps/api
.venv/Scripts/python -m pytest -q            # 427 tests
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m mypy app

cd ../web
npx tsc --noEmit
npx vitest run                                # 61 tests
```

**427 API tests collected. 408 pass on SQLite with nothing else running; the
other 19 skip** — they need the Postgres and MinIO containers, because they
exercise things SQLite cannot (the consent ledger's append-only trigger, the
`CHECK` constraints, real presigned uploads). With Docker up you should see
427 pass and 0 skip. A skip count of 19 means Docker is down, not that
something is broken.

⚠️ **If you run two `pytest` processes at once**, set `TEST_DB_PATH` on each
— they share one SQLite file by default and will corrupt each other's runs.
And if a run is killed mid-flight it leaves a `.db-journal` that produces
~10 spurious failures on the *next* run; delete the `.db`/`.db-journal` pair.

The browser suites under `apps/web/smoke/` need the full stack running
(Postgres, Redis, MinIO, API, worker, Vite) and are a manual gate, not part
of CI — see `docs/runbooks/ci-cd.md` for why.

---

## Onboarding another developer

Everything above, minus the things only you have. In order:

1. **Clone, then follow Path A.** It needs no secrets from you — the seed's
   password and MFA secret are published in the repo on purpose, so there is
   no credential to hand over and no motive to reuse a real one.
2. **They do NOT need your `.env`.** They generate their own
   `PHI_ENCRYPTION_KEY`. Never send yours: it decrypts every patient name
   and note in your database. There is no cleanup if it leaks.
3. **For Path B they need their own Groq key.** Free tier, self-service.
   Don't share yours — the rate limit is per-key and you'll both hit it.
4. **Point them at these, in this order:**
   - `docs/implementation-checklist.md` — what exists and what is deliberately absent
   - `docs/decisions/README.md` — every non-obvious choice, with reasoning
   - `docs/progress/README.md` — what each phase actually built and what it caught

The decision records are the fastest way in. Most of this codebase's
surprises are documented there rather than discoverable from the code —
0030 (why grounding refuses to guess), 0035 (why the note vendor changed),
0039 (why a one-character edit can be disqualifying).

---

## What is genuinely not built

Being explicit so nobody goes looking:

- **Nothing is deployed.** No VM, domain, certificate or managed database
  exists. The topology is specified (`docs/runbooks/deployment.md`), not
  running.
- **No CI job has ever run on a GitHub runner.** The workflow was rehearsed
  locally; the first push is the first real run.
- **No alert reaches anyone.** The rules exist, delivery needs a Sentry
  account nobody has created.
- **Speaker diarization.** P0-3 asks for it; Whisper structurally cannot
  provide it, so every segment is `speaker_unknown` (decision 0018).
- **Dictated patient search.** P0-6 says "typed or dictated"; only typed
  works.
- **A patient merge tool.** A mistyped birthdate creates a duplicate, and
  nothing can currently merge them.
- **Any dashboard.** The pilot report is a JSON endpoint.

And the one that is not an engineering gap at all:

> ⚠️ **Legal has not cleared the RA 4200 consent script.** The consent
> mechanism is complete and tested; the words it reads out are a
> placeholder. **Nothing here should be used to record a real patient until
> counsel has approved that text** — recording a conversation without valid
> consent is a criminal matter under the Philippine Anti-Wiretapping Act,
> not a product bug. Testing with your own voice is fine.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Login returns 401 | Almost always a stale TOTP code. Generate a fresh one. |
| Login returns 422 | The email failed schema validation — check the domain is `staging.remedy.example`. |
| `/ready` is 503 | Postgres or Redis is down. `docker compose -f infra/docker-compose.yml ps` |
| Recording stalls at `uploaded` | The Celery worker isn't running, or `GROQ_API_KEY` is unset. |
| The app refuses to start in "production" | Working as designed — the boot guard refuses the published dev key. Set `ENVIRONMENT=development`. |
| Port 5173 already in use | An orphaned Vite process. `strictPort` makes it fail loudly rather than move silently. |
| `~10 spurious test failures` | A stale `.db-journal`. Delete the `.db` and `.db-journal` pair. |
| Docker won't connect | Docker Desktop isn't running. `docker ps` should not error. |
