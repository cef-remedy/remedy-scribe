<!-- artifact: https://claude.ai/code/artifact/3aefcc42-2b6a-41c6-9bdd-3b689a7f0f5e (docs/runbooks/deploy-free-tier.html) -->
# Deployment runbook — free tier (Netlify + Google Drive)

**Target:** a working demo, driven with your own voice. Not a patient pilot —
see §8 for why, and it is not negotiable by configuration.

Every part is a numbered step with something you can check afterwards.

**Two audiences:** **you**, owning the checklist, and **the engineer with the
Netlify account**, who needs only Part 3. Part 3 is written to be handed over
whole.

**Two stages**, and the order matters:

- **Stage 1** works with no Google account in existence.
- **Stage 2** waits for Drive credentials.

---

## 0. Decide the Drive setup before anything else

One decision shapes §5 and cannot be fixed later by configuration: **who owns
the recordings**. Google's own words:

> "Service accounts don't have storage quota and can't own files. Instead,
> they must upload files and folders into shared drives, or use OAuth 2.0 to
> upload items on behalf of a human user."
> — Google Drive API docs

So there are two workable setups, and they are not equally good.

| | **A** · Service account + Shared Drive | **B** · A person's OAuth grant |
|---|---|---|
| Needs | Workspace with Shared Drives | any Google account |
| Who owns the recordings | **the organisation** | a named employee (unless they upload into a shared drive) |
| If that person leaves | nothing happens | **audio leaves with them** |
| Storage pool | org pooled quota (2 TB+ on Business Standard) | their personal 15 GB, **shared with Gmail** |
| Can it break by itself? | no human can revoke it | **they can revoke the grant** |
| Setup effort | ~10 min, §5A | ~10 min, §5B |

### How to tell which you have, in five seconds

Open Drive and look at the left sidebar. **If "Shared drives" is there** —
with a "Create a shared drive" button — the account is on Business Standard
or above, and **Setup A is available. Use it.** Business Starter has no
Shared drives at all, so its absence means Setup B.

### ⚠️ Unchanged by either setup, and by every paid tier

Drive has **no presigned GET**, so audio playback is proxied through the API
rather than served straight from storage — PHI bytes cross the application
server on the way out. And Drive has **no lifecycle rules**, so expired audio
is deleted only by the Celery purge, which runs only while your always-on
machine is on. Neither is a free-tier limitation and no plan removes them.

> **Worth raising once, then dropping:** Cloudflare R2 is S3-compatible —
> four environment variables, *zero* code, 10 GB free with free egress,
> uploads *and* downloads stay direct, and `no-store` survives. Drive cost
> about two days of adapter work. Same $0. If the answer is still Drive, this
> runbook works.

---

## 1. The stack

| Piece | Service | Free tier | Notes |
|---|---|---|---|
| Frontend | **Netlify** | 300 credits/mo (~15 GB) | Engineer's account. Part 3. |
| API | **Render** web service | 750 instance-hrs/mo, 0.1 CPU / 512 MB | Sleeps after 15 min idle |
| Worker + Beat | **your own always-on machine** | free | No free host exists. §6. |
| Postgres | **Neon** | 512 MB, scale-to-zero | Demo-scale only — §8 |
| Redis | **Upstash** | 500 K commands/mo | ⚠️ Must raise the poll timeout |
| Audio | **Google Drive** | org pooled, or 15 GB personal | §0 and §5 |
| ASR + notes | **Groq** | free tier | Cannot carry a full consult |

---

## 2. Code status — done, except what needs real credentials

Nothing in this runbook is waiting on code. `STORAGE_BACKEND=drive` selects
the Drive backend; `s3` — still the default — is untouched, so Stage 1 runs
on an unconfigured default and works.

### Which steps actually need Drive

| Step | Needs Drive? |
|---|---|
| Netlify build, rewrite, SPA fallback (§3) | **No** |
| Accounts, Neon, Upstash, Groq (§4) | **No** |
| Render deploy, migrations, seed, worker (§6) | **No** |
| Login, worklist, patient search, note review (§7) | **No** |
| Grounding *highlights* — the transcript half | **No** |
| **Recording → upload → transcription** | **Yes** |
| **Audio playback in grounding** | **Yes** |
| **Retention purge of audio** | **Yes** |

### What shipped

- [x] **Drive storage adapter behind a dispatcher** — `storage_drive.py`,
      selected by `STORAGE_BACKEND`, with the S3 code moved verbatim to
      `storage_s3.py`. No call site changed.
- [x] **Both auth grants** — a service account (JWT bearer, preferred when
      set) and a human refresh token. Setup A and Setup B are a configuration
      choice, not a code change.
- [x] **Proxied playback route** — `GET /encounters/{id}/audio`, used
      automatically when the backend cannot presign. Honours `Range`, sets
      `Cache-Control: no-store`, and runs the *same* audio-state check, so a
      withdrawn recording is refused identically on either backend.
- [x] **Shared-drive visibility** — every Drive call opts in, including the
      file lookup that needs *two* flags (`supportsAllDrives` **and**
      `includeItemsFromAllDrives`). Without them Drive answers `200` with an
      empty list, so a shared drive reads as empty: playback would report
      every recording missing, and a consent withdrawal would report success
      having deleted nothing.
- [x] **Client sends `Content-Range` and accepts `308`**, chunking to the size
      the server reports rather than assuming S3's 5 MiB floor.
      `308 Resume Incomplete` is not `response.ok`, so without this every
      multi-part Drive upload failed at the first chunk.
- [x] **`audio_upload_id` widened 128 → 512** (migration `c7e8f9a0b1d2`) —
      Drive stores the whole session URI there.

| Function | Drive equivalent |
|---|---|
| `build_audio_object_key` | a filename + parent folder id |
| `create_multipart_upload` | POST `uploadType=resumable` → session URI |
| `presign_part_upload` | return the session URI (browser PUTs with `Content-Range`) |
| `list_uploaded_parts` | empty `PUT` with `Content-Range: */size` → `308` + `Range` |
| `complete_multipart_upload` | the final chunk completes it; verify with `files.get` |
| `abort_multipart_upload` | `DELETE` the session URI |
| `head_object` | `files.get?fields=id,size` |
| `download_object` | `files.get?alt=media` (server-side, for ASR) |
| `delete_object` | `files.delete` — not trash, which would leave a withdrawn patient's recording recoverable for 30 days |
| `presign_audio_playback` | ⚠️ **impossible** — raises, and the proxy route takes over |
| `ensure_bucket_configured` | create/verify the folder; no lifecycle rules exist |

### CORS on Google's upload host — verified empirically

The preflight reflects an arbitrary `Origin`, allows `PUT`, and allows the
`content-range` header, so the browser really can upload direct. Google
documents none of this, so it was tested before any code was written:

```
Access-Control-Allow-Origin:  https://example.netlify.app
Access-Control-Allow-Methods: DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT
Access-Control-Allow-Headers: content-range
```

⚠️ Note what is **absent**: no `Access-Control-Expose-Headers`, so JavaScript
cannot read the `Range` header off a `308`. Resume flows through our own
`GET /upload/parts` — server-to-server, no CORS — as it already did on S3.
A client-side read silently returns nothing.

### Unverified, because it needs real credentials

The adapter is tested against a stubbed Drive. Check these first once §5 is
done.

- [ ] A browser really can `PUT` to a live session URI.
- [ ] `Range` works end to end through the playback proxy.
- [ ] A resumed upload skips the right chunks.
- [ ] **Deletion really deletes.** Withdraw consent, then look in Drive: the
      file must be gone, **not in Trash**. This is the one that silently
      fails if the service account's shared-drive role is too low.

---

## 3. FOR THE ENGINEER — Netlify (hand this section over whole)

You are deploying **only the frontend**: a static Vite/React bundle. The
backend runs elsewhere and you need none of its credentials.

`apps/web/netlify.toml` is already in the repo and carries the base
directory, build command, publish directory, Node version, the environment
variable, both rewrites in the order that matters, and the headers. **One
line needs editing.**

1. [ ] **Get the Render hostname** from whoever owns the backend. It looks
       like `remedy-api.onrender.com`.
2. [ ] **Edit one line in `apps/web/netlify.toml`** — replace `REPLACE-ME`
       with that hostname.
       ```toml
       [[redirects]]
         from   = "/api/*"
         to     = "https://<render-service>.onrender.com/api/:splat"
         status = 200          # 200, not 301 — a rewrite, not a redirect
         force  = true
       ```
       A `200` rewrite keeps the URL in the address bar and fetches behind
       the scenes, so the API is **same-origin**: no CORS, and the session
       cookie keeps `SameSite=lax` instead of being weakened to `None`.
3. [ ] **Create the Netlify site from the repo.** Set **Base directory** to
       `apps/web`; everything else comes from the file.
       → *You should see* a build log running `npm run build`, publishing
       `apps/web/dist`, on Node 20.
4. [ ] **Do _not_ add `VITE_API_BASE_URL` in the Netlify UI.** The file
       already sets it to `/`, and a UI value **overrides the file**.
       Pointing it at the Render hostname is the tempting mistake and the
       wrong one: it makes the API cross-site, forcing `SameSite=None` on the
       refresh cookie and putting CORS back on the critical path. Vite
       inlines it at **build** time, so a change needs a **rebuild**.
       → *You should see* no `VITE_` variables listed at all.
5. [ ] **Set up an uptime pinger — required, not optional.** Ping
       `https://<render-service>.onrender.com/health` every **10 minutes**.
       Netlify's proxy times out at **26 seconds** and a spun-down Render
       service takes about **60** to wake, so the first request after 15
       quiet minutes *fails* rather than being slow.
       → *Note* a 31-day month awake is 744 hours against Render's 750. It
       fits, but only for **one** free web service.
6. [ ] **Restrict who can reach the site.** The app requires login, but the
       bundle is world-readable. Add Netlify password protection or keep the
       URL unadvertised.
7. [ ] **Check a deep link survives a hard refresh** — open
       `https://<site>/notes/anything` and reload.
       → *You should see* the app, not a 404.
8. [ ] **Send two things back:** the **Netlify site URL** (needed for the
       backend's CORS list even with the proxy), and confirmation that
       `https://<site>/api/v1/../health` answers *through the rewrite*.
       → *You should see* `{"status":"ok"}`.

---

## 4. Accounts and secrets

1. [ ] **Neon** (neon.tech → New project) — copy the **pooled** connection
       string, append `?sslmode=require`.
       → *You should see* a string starting `postgresql://` containing
       `-pooler`.
2. [ ] **Upstash Redis** (upstash.com → Redis → Create database) — copy the
       **`rediss://` TCP** URL.
       → ⚠️ **Not** the REST URL: Celery needs blocking `BRPOP`, which
       Upstash explicitly does not support over REST, and the failure is a
       worker that connects and never receives a job.
3. [ ] **Groq API key** (console.groq.com → API Keys).
4. [ ] **Generate the PHI encryption key.**
       ```bash
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
       ```
       → ⚠️ **Back this up before going further.** Lose it and every note,
       name and transcript is unrecoverable. There is no reset.
5. [ ] **Generate a JWT secret** — different from the PHI key. This one can
       be rotated freely; it only logs people out.
       ```bash
       python -c "import secrets; print(secrets.token_urlsafe(48))"
       ```

### Deploy in two stages, and not the other way round

| Stage | Do | Then verify |
|---|---|---|
| **1** | Deploy with `STORAGE_BACKEND` unset (defaults to `s3`) and no S3 configured. | All of §3, all of §6, §7's first six checks. |
| **2** | Do §5, then set `STORAGE_BACKEND=drive` and redeploy. | The recording loop, and §2's unverified list. |

Doing both at once makes a Drive problem and a Netlify problem
indistinguishable on the first deploy — they produce similarly vague
symptoms, and you end up debugging two unknowns against each other.

---

## 5. Google Drive — pick Setup A or Setup B

From §0: **A** if the account has Shared drives, **B** otherwise. Do one, not
both.

### Setup A · Service account + Shared Drive (Stage 2)

Roughly ten minutes, and it takes every human out of the loop.

1. [ ] **Create or pick a Google Cloud project**
       (console.cloud.google.com → project picker → New Project). Name it
       something a stranger would recognise a year from now, e.g.
       `remedy-scribe`.
2. [ ] **Enable the Google Drive API** (APIs & Services → Library → search
       "Google Drive API" → Enable).
       → *You should see* it under "Enabled APIs & services".
3. [ ] **Create the service account** (IAM & Admin → Service Accounts →
       Create service account), named `remedy-scribe-storage`. **Skip the
       "Grant this service account access to project" step entirely** — it
       needs no IAM role. Its Drive access comes from shared-drive membership
       in step 6, and adding project roles here grants nothing useful while
       widening the blast radius if the key leaks.
       → *You should see* an address like
       `remedy-scribe-storage@remedy-scribe.iam.gserviceaccount.com`. Copy
       it; step 6 needs it.
4. [ ] **Create a JSON key** (the service account → Keys → Add key → Create
       new key → JSON). A `.json` file downloads. **This is a private key and
       Google keeps no copy** — treat it like the PHI key.
       → *You should see* a file containing `"client_email"` and
       `"private_key"`.
5. [ ] **Create the Shared Drive** (Drive → Shared drives → Create a shared
       drive), named e.g. `Remedy Scribe`. This is what will *own* the
       recordings.
6. [ ] **Add the service account as a member — as `Content manager`**
       (the shared drive → Manage members → paste the address from step 3).
       → ⚠️ **The role matters and the wrong one fails silently in the worst
       direction.** A *Contributor* can upload but **cannot delete**, so
       recording would work perfectly while consent withdrawal and the
       retention purge quietly removed nothing. Content manager can delete.
       If a deletion still returns 403, raise it to *Manager*.
       → *You should see* the service account listed as a member. It never
       signs in and never appears "active" — that is normal.
7. [ ] **Make a folder inside the shared drive and copy its id.** Create e.g.
       `audio`, open it, take the id from the URL:
       ```
       https://drive.google.com/drive/folders/1AbC...XyZ
                                               ^^^^^^^^^^ this part
       ```
       → ⚠️ **Not the drive root.** A service account has no "My Drive" of
       its own, so an unset folder id has nowhere to write.
8. [ ] **Set the environment variables on Render:**
       ```
       GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON=<the entire contents of the .json file>
       GOOGLE_DRIVE_FOLDER_ID=1AbC...XyZ
       STORAGE_BACKEND=drive
       ```
       Paste the JSON whole, newlines and all — Render accepts multi-line
       values, and keeping it as issued means nobody picks fields out of it
       by hand. Leave the three `GOOGLE_DRIVE_CLIENT_*` / `_REFRESH_TOKEN`
       variables unset; if both are present the service account wins, but
       there is no reason to keep a human's grant lying around.
9. [ ] **Delete the downloaded `.json` from your laptop.** Once Render has
       it, the copy in Downloads is only a way to lose it. If you need it
       again, make a new key and delete the old one.

### Setup B · A person's OAuth grant (Stage 2)

⚠️ **Governance, not a technical detail.** The recordings will live in one
employee's Google account and be **owned by them**. If they leave, hit their
quota, or revoke access, the audio goes with them — and Gmail shares the same
15 GB, so filling Drive with audio also stops that person receiving email.
State this to your supervisor rather than absorbing it.

1. [ ] **Create a Cloud project and enable the Drive API** — Setup A steps
       1–2.
2. [ ] **Create an OAuth client** (APIs & Services → Credentials → Create
       credentials → OAuth client ID → Web application). Note the client id
       and secret.
3. [ ] **Complete the consent flow once**, as the person whose Drive holds
       the audio, requesting scope
       `https://www.googleapis.com/auth/drive`. Keep the **refresh token**.
       → ⚠️ **No helper script exists for this yet** — it is the one step in
       this runbook without one. Ask if you want it built.
4. [ ] **Create a folder and copy its id.** If the account *does* have shared
       drives, put the folder in one anyway: the files are then owned by the
       organisation even though a person authorised the app. It costs nothing
       and removes the worst of the problem above.
5. [ ] **Set the environment variables on Render:**
       ```
       GOOGLE_DRIVE_CLIENT_ID=…
       GOOGLE_DRIVE_CLIENT_SECRET=…
       GOOGLE_DRIVE_REFRESH_TOKEN=…
       GOOGLE_DRIVE_FOLDER_ID=…
       STORAGE_BACKEND=drive
       ```

---

## 6. Deploy

1. [ ] **Create the Render web service** (render.com → New → Web Service →
       from `apps/api/Dockerfile`). The image honours `$PORT`, so no
       start-command override is needed.
2. [ ] **Set the environment variables:**
       ```
       ENVIRONMENT=production
       DATABASE_URL=postgresql+psycopg://…neon…?sslmode=require
       REDIS_URL=rediss://…upstash…:6379
       PHI_ENCRYPTION_KEY=<from §4 step 4 — NOT the one in .env.example>
       JWT_SECRET=<from §4 step 5>
       REFRESH_COOKIE_SECURE=true            # the guard refuses false in production
       REFRESH_COOKIE_SAMESITE=lax           # 'lax' works *because* of the Netlify rewrite
       CORS_ALLOW_ORIGINS=https://<site>.netlify.app   # no localhost — the guard refuses it
       GROQ_API_KEY=…
       NOTE_GENERATOR_PROVIDER=groq
       S3_PROVISION_BUCKET_ON_STARTUP=false
       AUDIO_RETENTION_DAYS=90

       # Stage 2 only — leave unset for Stage 1.
       # STORAGE_BACKEND=drive
       # …plus whichever set §5 told you to use
       ```
       → *If it refuses to boot*, that is the guard working. It prints
       **every** problem at once rather than one per restart.
3. [ ] **Run the migrations yourself — never on boot.**
       ```bash
       cd apps/api
       DATABASE_URL=<neon-url> .venv/Scripts/python -m alembic upgrade head
       ```
       Deliberate: three processes share this image, and migrating on boot
       means three racing for the same lock.
4. [ ] **Seed a demo account.**
       ```bash
       REMEDY_ALLOW_SYNTHETIC_SEED=1 ENVIRONMENT=staging DATABASE_URL=<neon-url> \
         .venv/Scripts/python scripts/seed_staging.py --yes
       ```
       ⚠️ `ENVIRONMENT` must **not** be `production` here — the seed refuses
       to run against anything that looks real. Set Render to `production`
       *after* this.
       → *You should see* working credentials printed, including a live TOTP
       code.
5. [ ] **Start the Celery worker on an always-on machine you control** — a
       clinic PC, a spare laptop, anything that stays on. There is no free
       hosted option: Render's background workers and cron jobs are
       paid-only, Railway's free credit is consumed in hours, and Fly retired
       its free tier. The worker needs **no inbound network**, only outbound
       access to Neon, Upstash, Drive and Groq.
       ```bash
       cd apps/api
       export DATABASE_URL=<neon-url> REDIS_URL=<upstash-url> GROQ_API_KEY=…
       export PHI_ENCRYPTION_KEY=<the same key as Render>

       .venv/Scripts/python -m celery -A app.tasks.celery_app worker \
         --loglevel=info --pool=solo \
         --broker-transport-options '{"socket_timeout": 60, "brpop_timeout": 30}'
       ```
       ⚠️ **That `brpop_timeout` is not optional.** Celery's default
       ~1-second blocking read produces about **2,592,000 commands a month
       against Upstash's 500,000 — 5× over, with the worker completely
       idle.** At 30 seconds it is about 86,000.
6. [ ] **Start Beat — exactly one, ever.**
       ```bash
       .venv/Scripts/python -m celery -A app.tasks.celery_app beat --loglevel=info
       ```
       → ⚠️ Two Beat processes double-fire the retention purge, and **that
       purge deletes patient data**. Before restarting it, confirm the old
       one is actually dead.
7. [ ] **Check the Upstash counter an hour later.**
       → *You should see* the command count growing slowly — dozens, not
       thousands. Thousands means step 5's transport options did not take.

---

## 7. Verify, in this order

Each one isolates a different failure, so the first thing that breaks tells
you where the problem is.

1. [ ] `https://<render>.onrender.com/health` → `{"status":"ok"}`
2. [ ] `https://<render>.onrender.com/ready` → `{"status":"ready"}` with both
       `database` and `redis` `ok`. A 503 *names* which one is down.
3. [ ] `https://<site>.netlify.app/api/v1/../health` → the same JSON,
       **through the rewrite**. A timeout means the pinger isn't running and
       Render was asleep.
4. [ ] **Log in** on the Netlify URL with the seeded credentials.
5. [ ] **Reload the page while logged in.** Being thrown back to the login
       screen means the session cookie is not surviving — the rewrite isn't
       in front of `/api/*`, or `VITE_API_BASE_URL` was overridden in the
       Netlify UI.
6. [ ] **Open a seeded note and click a line** → the transcript passage it
       cites appears. This is the whole grounding mechanism working, and it
       needs no audio.
7. [ ] *(Stage 2)* **Record 20 seconds** and watch the worker log:
       `transcribe_encounter` → `generate_note`, and the file appears in the
       Drive folder.
8. [ ] *(Stage 2)* **Play a cited passage** → you hear that moment only;
       playback stops at the end of the citation.
9. [ ] *(Stage 2)* **Withdraw consent, then look in Drive** → the file must
       be gone, and **not sitting in Trash**. If it is still there, the
       service account's shared-drive role is too low; go back to §5A step 6.

---

## 8. What this deployment cannot do

State these plainly to anyone who asks what they are looking at.

- **It is not for patients.** No vendor here signs a BAA on a free tier — not
  Groq, not Upstash, and not a free Google account. **And Legal has not
  cleared the RA 4200 consent script**, which is a criminal-liability
  question under the Anti-Wiretapping Act, not a product gap. Your own voice
  is fine. A patient is not.
- **Free tier is demo scale, and here is the number.** Neon's 512 MB is
  exceeded by **transcripts alone** at real volume: 20 consults/day × 20 min
  is roughly **415 MB** of encrypted transcript in a 90-day window, before
  notes, revisions or the audit log. Expect a few dozen encounters, not a
  clinic month.
- **Groq's free tier cannot carry a full consultation.** ~8,000 tokens/min
  against a 10–20k-token transcript sent in one call. Short recordings work.
- **First request after idle is slow or fails.** Render sleeps at 15 min;
  Neon scales to zero at 5. The pinger fixes the API, not Neon's cold start.
- **Playback proxies PHI through the API**, which the S3 path was built to
  avoid, on a 0.1 CPU / 512 MB instance.
- **Retention has no storage-layer backstop.** Drive has no lifecycle rules,
  so only the Celery purge deletes expired audio — and it runs only while
  your always-on machine is on.
- **Nothing is alerting.** The alert rules exist; delivery needs a Sentry
  account nobody has created. If the worker dies at 2 a.m., nobody is told.
