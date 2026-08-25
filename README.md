# Remedy Scribe

In-clinic AI consultation note-taker. See `remedy-scribe-prd.md` for
requirements, `remedy-scribe-roadmap.md` for sequencing, and
`docs/tech-stack.md` for the stack decision and rationale behind every
piece below.

## Layout

```
apps/
  api/      FastAPI backend (Python) — patients, encounters, consent
            ledger, note lifecycle, ASR + note-generation pipeline
  mobile/   React Native app (Expo, TypeScript) — the doctor's client
infra/
  docker-compose.yml   postgres, redis, minio, api, worker — local dev
docs/
  tech-stack.md
```

## Backend (`apps/api`)

```bash
cd apps/api
python -m venv .venv
./.venv/Scripts/pip install -r requirements-dev.txt   # Windows; use .venv/bin on macOS/Linux
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the output into .env as PHI_ENCRYPTION_KEY
```

Bring up Postgres/Redis/MinIO, then migrate and run:

```bash
docker compose -f ../../infra/docker-compose.yml up -d postgres redis minio
./.venv/Scripts/python -m alembic upgrade head
./.venv/Scripts/python -m uvicorn app.main:app --reload
```

`GET /health` should return `{"status": "ok", ...}`. Run the test suite
(SQLite, no external services needed) with:

```bash
./.venv/Scripts/python -m pytest
```

**Note on local ports:** `infra/docker-compose.yml` publishes Postgres on
`5433`, Redis on `6380`, and MinIO on `9002`/`9003` — one host machine
this was built on already had native Postgres/Redis installs squatting on
the standard ports, silently swallowing Docker's forwarded traffic. If
your machine is clean, feel free to change the compose file back to the
standard ports; just keep `.env` in sync.

## Mobile (`apps/mobile`)

```bash
cd apps/mobile
npm install
npx expo start
```

Background audio capture (P0-2) needs native modules that plain Expo Go
doesn't expose — `expo-dev-client` is already installed; building a real
dev client (`npx expo run:android` / `eas build --profile development`)
requires an Android/iOS toolchain this scaffold doesn't assume you have.
`npx expo export --platform android` (headless, no device needed) is a
good smoke check that the JS bundle still builds.

## What's implemented vs. stubbed

The backend's data model, auth, RBAC, the note state machine, the
consent ledger (with a DB trigger enforcing append-only), patient
fuzzy-matching/dedup, and the Celery pipeline wiring are real and
tested. The ElevenLabs Scribe and Luna/Haiku note-generation calls are
stubbed behind their provider interfaces (`app/services/asr`,
`app/services/note_generation`) pending API keys and the Legal BAA/DPA
question in the roadmap's Open Questions — swapping in a real call is a
same-file change, not a redesign.
