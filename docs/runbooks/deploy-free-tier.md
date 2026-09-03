<!-- artifact: https://claude.ai/code/artifact/3aefcc42-2b6a-41c6-9bdd-3b689a7f0f5e (docs/runbooks/deploy-free-tier.html) -->
# Deployment checklist — free tier (Netlify + Google Drive)

**Target:** a working demo, driven with your own voice. Not a patient pilot —
see the last section for why, and it is not negotiable by configuration.

Two audiences: **you**, owning the checklist, and **the engineer with the
Netlify account**, who needs only Part 3. Part 3 is written to be handed over
whole.

---

## 0. Read this first — one blocker that config cannot solve

Google Drive was chosen for storage. Most of it works. **One part does not,
and it is Google's documented product behaviour, not a limit we can raise.**

> "Service accounts don't have storage quota and can't own files. Instead,
> they must upload files and folders into shared drives, or use OAuth 2.0 to
> upload items on behalf of a human user."
> — Google Drive API docs

And the escape hatch that would normally fix it is paid:

> "**Personal Google Accounts (@gmail.com) cannot create shared drives**"
> — same source

So on a free Google account there is **no service-account path**. The app
cannot hold its own Drive credentials and own its own files. What is left:

| Path | Works on free? | What it means in practice |
|---|---|---|
| Service account + its own quota | **No** | Documented as impossible. |
| Service account + Shared Drive | **No** | Shared Drive needs paid Workspace. |
| **OAuth as a human user** | **Yes** | A named person authorises the app once. Audio lands in *their* personal 15 GB and is **owned by them**, not by the clinic. |

**We proceed with OAuth-as-a-human**, because it is the only free path — but
be clear on what that means: the recordings live in one employee's Google
account. If they leave, or hit their quota, or revoke access, the audio goes
with them. That is a governance fact to state to your supervisor, not a
technical detail.

### Two further Drive limits worth knowing before the engineer starts

- **Uploads stay direct-to-storage.** Drive's resumable session URI can be
  PUT to by a browser with no credentials (verified against Google's own
  sample code), so the API still never sees audio bytes on the way *in*.
  ⚠️ But that URI lives **one week with no configurable expiry**, where our
  S3 presigned PUTs live minutes. It is a much longer-lived credential.
- **Downloads cannot stay direct.** Drive has **no presigned GET** — the
  only options are an OAuth-authenticated API call, or sharing the file with
  "anyone with the link", which for PHI is unacceptable. So audio playback
  must be **proxied through the API**, and `Cache-Control: no-store` (which
  we currently sign into the URL) has no Drive equivalent. This gives up a
  design property the S3 path existed to protect.

> **If the engineer will reconsider once:** Cloudflare R2 is S3-compatible,
> so it is **four environment variables and zero code**, gives 10 GB free
> with free egress, keeps uploads *and* downloads direct, and keeps
> `no-store`. Drive is ~2 days of adapter work plus the ownership problem
> above. Same $0. Raise it once; if the answer is still Drive, this checklist
> works.

---

## 1. The stack

| Piece | Service | Free tier | Notes |
|---|---|---|---|
| Frontend | **Netlify** | 300 credits/mo (~15 GB) | Engineer's account. Part 3. |
| API | **Render** web service | 750 instance-hrs/mo, 0.1 CPU / 512 MB | Sleeps after 15 min idle |
| Worker + Beat | **your own always-on machine** | free | No free host exists. See §5. |
| Postgres | **Neon** | 512 MB, scale-to-zero | Demo-scale only — see §6 |
| Redis | **Upstash** | 500 K commands/mo | ⚠️ Must raise the poll timeout |
| Audio | **Google Drive** | 15 GB, shared with Gmail | Needs the adapter in §2 |
| ASR + notes | **Groq** | free tier | Cannot carry a full consult |

---

## 2. Code work — **done**, except what needs real credentials

The Drive adapter is written, tested and merged. `STORAGE_BACKEND=drive`
selects it; `s3` (the default) is untouched.

### What the deployment checklist actually needs it for

Worth being precise, because most of the checklist does **not** depend on
storage at all:

| Checklist step | Needs the adapter? |
|---|---|
| Netlify build, rewrite, SPA fallback (§3) | **No** |
| Render deploy, `$PORT`, boot guard (§5) | **No** |
| Neon connect + migrations (§5) | **No** |
| Login, worklist, patient search, note review (§6) | **No** |
| Grounding *highlights* — the transcript half | **No** |
| **Recording → upload → transcription** | **Yes** |
| **Audio playback in grounding** | **Yes** |
| **Retention purge of audio** | **Yes** |

So the engineer can deploy and verify everything through *"log in and open
a seeded note"* before Drive credentials exist. The core loop is what
waits.

- [x] **Google Drive storage adapter** — `app/services/storage_drive.py`,
      selected by `STORAGE_BACKEND`. `storage.py` became a dispatcher and
      the S3 code moved verbatim to `storage_s3.py`, so no call site
      changed and the existing tests kept working. 20 tests cover the
      protocol translation.

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
      | `delete_object` | `files.delete` |
      | `presign_audio_playback` | ⚠️ **impossible** — needs a proxy route instead |
      | `ensure_bucket_configured` | create/verify the folder; no lifecycle rules exist |

- [x] **Proxied playback route** — `GET /encounters/{id}/audio`, which the
      `audio-url` endpoint now falls back to automatically when the backend
      cannot presign. Honours `Range` (so the grounding UI still plays one
      passage rather than pulling the whole consultation), sets
      `Cache-Control: no-store` by hand, and runs the *same* audio-state
      check, so a withdrawn recording is refused with the same words on
      either backend.
- [x] **Client chunks to the size the server reports** rather than assuming
      S3's 5 MiB floor, and now sends `Content-Range` and treats Drive's
      **`308 Resume Incomplete` as success**. That last one mattered: the
      client checked `response.ok`, which is false for 308, so *every*
      multi-part Drive upload would have failed at the first chunk.
- [x] **`audio_upload_id` widened 128 → 512** (migration `c7e8f9a0b1d2`).
      Drive stores the whole resumable session URI there, and a truncated
      one fails at the first chunk with an error naming nothing.
- [x] **CORS on Google's upload host — verified empirically.** The preflight
      reflects an arbitrary `Origin`, allows `PUT`, and allows the
      `content-range` header, so the browser really can upload direct.
      Google documents none of this, so it was tested rather than assumed:

      ```
      Access-Control-Allow-Origin: https://example.netlify.app
      Access-Control-Allow-Methods: DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT
      Access-Control-Allow-Headers: content-range
      ```

      ⚠️ Note there is **no `Access-Control-Expose-Headers`**, so JavaScript
      cannot read the `Range` header off a 308. Resume therefore goes
      through our own `GET /upload/parts` (server-to-server, no CORS) — as
      it already did. Don't let anyone "simplify" it to a client-side read.

- [ ] **Store the OAuth refresh token** for the human account. Set
      `GOOGLE_DRIVE_CLIENT_ID`, `_SECRET`, `_REFRESH_TOKEN`, `_FOLDER_ID`.
- [ ] ⚠️ **Retention loses a layer, and this cannot be fixed in code.**
      Drive has no bucket lifecycle rules, so decision 0033's storage-layer
      backstop is gone and *only* the Celery purge deletes expired audio —
      which runs only while your always-on machine is on.

### Still unverified, because it needs real credentials

The adapter is tested against a stubbed Drive. These need a real account
and should be the **first** thing checked after credentials exist:

- [ ] A browser really can PUT a chunk to a live session URI (the CORS
      preflight says yes; an actual upload proves it).
- [ ] `Range` really works through the playback proxy end to end — click a
      note line, hear that passage, not the whole consultation.
- [ ] A resumed upload after a dropped connection skips the right chunks.
- [ ] The 15 GB fills as expected, and Gmail is in the same quota.

---

## 3. FOR THE ENGINEER — Netlify (hand this section over whole)

You are deploying **only the frontend**: a static Vite/React bundle. The
backend runs elsewhere; you do not need its credentials.

**`apps/web/netlify.toml` is already in the repo and carries all of this** —
base directory, build command, publish directory, Node version, the
environment variable, the rewrites and the headers. **Exactly one line needs
editing.**

- [ ] **Edit that one line.** In `apps/web/netlify.toml`, replace
      `REPLACE-ME` with your Render hostname:
      ```toml
      [[redirects]]
        from   = "/api/*"
        to     = "https://<render-service>.onrender.com/api/:splat"
        status = 200          # 200, not 301 — a rewrite, not a redirect
        force  = true
      ```
      A `200` rewrite keeps the URL in the address bar and fetches behind the
      scenes, so the API is **same-origin**. That is deliberate: it means no
      CORS, and the session cookie keeps `SameSite=lax` instead of being
      weakened to `None`.

- [ ] **Point Netlify at it.** If you create the site from the repo, Netlify
      reads `apps/web/netlify.toml` once **Base directory** is set to
      `apps/web`. Confirm the build log shows `npm run build` and a publish
      directory of `apps/web/dist`.

- [ ] ⚠️ **Do not set `VITE_API_BASE_URL` in the Netlify UI.** The file
      already sets it to `/`, and a UI value **overrides** the file. Pointing
      it at the Render hostname is the tempting mistake and the wrong one: it
      makes the API cross-site, which forces `SameSite=None` on the refresh
      cookie and puts CORS back on the critical path. Vite also inlines it at
      **build** time, so changing it later needs a **rebuild**, not a
      redeploy.
      *(If it is somehow lost anyway, a production build now falls back to
      `/` rather than `http://localhost:8000` — but do not rely on that; a
      wrong explicit value still wins.)*

- [ ] ⚠️ **Then set up an uptime pinger, and treat it as required, not
      optional.** Netlify's proxy **times out after 26 seconds**; a
      spun-down Render free service takes **about 60 seconds** to wake. So
      the first request after 15 minutes of quiet *fails* rather than being
      slow. Point any free pinger (UptimeRobot, cron-job.org) at
      `https://<render-service>.onrender.com/health` every **10 minutes**.
      A 31-day month awake is 744 hours against Render's 750 — it fits, but
      only for **one** free web service, so don't add a second.

- [ ] **Note the deploy is public.** The app requires login, but the bundle
      is world-readable. Add Netlify password protection or keep the URL
      unadvertised.

- [ ] **Confirm the SPA fallback works** — deep links like `/notes/<id>`
      must serve `index.html`. Vite + Netlify usually handles this; if a
      refresh on a deep link 404s, add:
      ```toml
      [[redirects]]
        from = "/*"
        to = "/index.html"
        status = 200
      ```
      **after** the `/api/*` rule — order matters, first match wins.

- [ ] Send back: the **Netlify site URL** (needed for the backend's CORS
      list, even with the proxy) and confirmation that
      `https://<site>/api/v1/../health` returns `{"status":"ok"}` through
      the rewrite.

---

## 4. Accounts and secrets

- [ ] **Neon** — create a project, copy the pooled connection string.
      Append `?sslmode=require`.
- [ ] **Upstash Redis** — create a database, copy the **`rediss://` TCP**
      URL. ⚠️ **Not** the REST URL: Celery needs blocking `BRPOP`, which
      Upstash explicitly does not support over REST.
- [ ] **Groq** — API key.
- [ ] **Google Cloud project** — enable the Drive API, create an **OAuth
      client (Web application)**, and complete the consent flow **once** as
      the human whose Drive will hold the audio. Store the refresh token.
- [ ] **Generate a fresh PHI key** and keep it somewhere it cannot be
      casually deleted:
      ```bash
      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
      ```
      ⚠️ **Lose this and every note, name and transcript is unrecoverable.**
      There is no reset. Back it up before the first real recording.

### Suggested order, given the adapter is done but unproven

The storage backend gates only the recording loop, so deploy in this order
and you get a working, checkable app before Drive credentials exist:

1. **Deploy with `STORAGE_BACKEND=s3` left at its default and no S3
   configured.** Everything except recording works, so §3's Netlify steps,
   §5's Render deploy and §6's first five checks can all be verified and
   signed off.
2. **Add Drive credentials and flip to `STORAGE_BACKEND=drive`.** Then work
   the "still unverified" list in §2 — a real browser PUT, `Range` through
   the playback proxy, a resumed upload.

Doing it the other way round means a Drive problem and a Netlify problem
are indistinguishable on the first deploy.

### Environment variables for the Render service

The production boot guard **refuses to start** if any of these are wrong, and
prints every problem at once. A refused boot is the guard working.

```
ENVIRONMENT=production
DATABASE_URL=postgresql+psycopg://…neon…?sslmode=require
REDIS_URL=rediss://…upstash…:6379
PHI_ENCRYPTION_KEY=<the key you just generated — NOT the one in .env.example>
JWT_SECRET=<fresh random, 32+ bytes>
REFRESH_COOKIE_SECURE=true            # guard refuses false in production
REFRESH_COOKIE_SAMESITE=lax           # 'lax' works *because* of the Netlify rewrite
CORS_ALLOW_ORIGINS=https://<site>.netlify.app   # no localhost — the guard refuses it
GROQ_API_KEY=…
NOTE_GENERATOR_PROVIDER=groq
S3_PROVISION_BUCKET_ON_STARTUP=false
AUDIO_RETENTION_DAYS=90

# Storage. Leave unset (defaults to "s3") for step 1 above.
STORAGE_BACKEND=drive
GOOGLE_DRIVE_CLIENT_ID=…
GOOGLE_DRIVE_CLIENT_SECRET=…
GOOGLE_DRIVE_REFRESH_TOKEN=…
GOOGLE_DRIVE_FOLDER_ID=…        # a folder, not the account root — a human
                                # will need to find these files one day
```

---

## 5. Deploy

- [ ] **Render web service**, from `apps/api/Dockerfile`.
      The image now honours `$PORT`, so no start-command override is needed.
- [ ] **Run migrations as an explicit step — never on boot.** From your
      machine, pointed at the Neon URL:
      ```bash
      cd apps/api && DATABASE_URL=<neon-url> .venv/Scripts/python -m alembic upgrade head
      ```
      This is deliberate (checklist 5.1): three processes share this image,
      and a migration on boot means three racing for the same lock.
- [ ] **Seed a demo account** so someone can log in:
      ```bash
      REMEDY_ALLOW_SYNTHETIC_SEED=1 DATABASE_URL=<neon-url> \
        .venv/Scripts/python scripts/seed_staging.py --yes
      ```
      ⚠️ `ENVIRONMENT` must **not** be `production` for this — the seed
      refuses to run against anything that looks real. Run it with
      `ENVIRONMENT=staging`, then set the Render service to `production`.
      It prints working credentials, including a live TOTP code.

- [ ] **Start the worker and beat on an always-on machine you control** —
      a clinic PC, a spare laptop, anything that stays on. There is no free
      hosted option: Render's background workers and cron jobs are paid-only,
      Railway's free credit is consumed in hours, and Fly retired its free
      tier. The worker needs **no inbound network**, only outbound access to
      Neon, Upstash, Drive and Groq:

      ```bash
      cd apps/api
      export DATABASE_URL=<neon-url> REDIS_URL=<upstash-url> GROQ_API_KEY=…
      export PHI_ENCRYPTION_KEY=<the same key as Render>

      # one terminal — the worker
      .venv/Scripts/python -m celery -A app.tasks.celery_app worker \
        --loglevel=info --pool=solo

      # another — beat. EXACTLY ONE of these, ever.
      .venv/Scripts/python -m celery -A app.tasks.celery_app beat --loglevel=info
      ```

- [ ] ⚠️ **Raise the Redis poll timeout, or Upstash's free tier dies in
      days.** Celery's default ~1-second blocking read produces about
      **2,592,000 commands a month against a 500,000 budget — 5× over, with
      the worker completely idle.** At a 30-second timeout it is about
      86,000. Set it on the worker:
      ```
      --broker-transport-options '{"socket_timeout": 60, "brpop_timeout": 30}'
      ```
      Verify in Upstash's dashboard after an hour that command count is
      growing slowly, not by thousands.

- [ ] ⚠️ **Exactly one beat process.** Two double-fire the retention purge,
      and that purge **deletes patient data**. If you restart it, confirm the
      old one is dead first.

---

## 6. Verify, in this order

- [ ] `https://<render>.onrender.com/health` → `{"status":"ok"}`
- [ ] `https://<render>.onrender.com/ready` → `{"status":"ready"}` with both
      `database` and `redis` `ok`. A 503 here names which dependency is down.
- [ ] `https://<site>.netlify.app/api/v1/../health` → same, **through the
      rewrite**. If this times out, the pinger isn't working.
- [ ] Log in on the Netlify URL with the seeded credentials.
- [ ] **Reload the page after logging in.** If you are thrown back to the
      login screen, the session cookie is not surviving — the rewrite isn't
      in front of `/api/*`, or `VITE_API_BASE_URL` was overridden in the
      Netlify UI and is not `/`.
- [ ] Open a seeded note; click a line; confirm a transcript passage appears.
- [ ] Record 20 seconds and watch the worker log:
      `transcribe_encounter` → `generate_note`.
- [ ] Check the Upstash command counter an hour later (previous section).

---

## 7. What this deployment cannot do

State these plainly to anyone who asks what they are looking at.

- **It is not for patients.** No vendor here signs a BAA on a free tier —
  not Groq, not Upstash, not Cloudflare, and a free personal Google account
  cannot have one for Drive. **And Legal has not cleared the RA 4200 consent
  script**, which is a criminal-liability question under the Anti-Wiretapping
  Act, not a product gap. Your own voice is fine. A patient is not.
- **Free tier is demo scale, and here is the number.** Neon's 512 MB is
  exceeded by **transcripts alone** at real volume: 20 consults/day × 20 min
  is roughly **415 MB** of encrypted transcript in a 90-day window, before
  notes, revisions or the audit log. Expect a few dozen encounters, not a
  clinic month.
- **Groq's free tier cannot carry a full consultation.** ~8,000 tokens/min
  against a 10–20k-token transcript sent in one call. Short recordings work.
- **First request after idle is slow or fails.** Render sleeps at 15 min;
  Neon scales to zero at 5. The pinger fixes the API, not Neon's cold start.
- **Audio lives in one person's Google account**, owned by them (§0).
- **Playback proxies PHI through the API**, which the S3 path was built to
  avoid, on a 0.1 CPU / 512 MB instance.
- **Nothing is alerting.** Phase 5.2's rules exist; delivery needs a Sentry
  account nobody has created. If the worker dies at 2 a.m., nobody is told.
- **Retention loses its storage-layer backstop** — no Drive lifecycle rules,
  so only the Celery purge deletes expired audio, and that purge only runs
  while your always-on machine is on.
