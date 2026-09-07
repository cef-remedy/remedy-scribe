<!-- artifact: https://claude.ai/code/artifact/3aefcc42-2b6a-41c6-9bdd-3b689a7f0f5e (docs/runbooks/deploy-free-tier.html) -->
# Deployment runbook — free tier (Netlify + Google Drive)

**Target:** getting this live for real use — Netlify, Render, Neon, Upstash
and Google Drive — not a rehearsal. Not a patient pilot — see §8 for why, and
it is not negotiable by configuration.

**Two audiences:** **you**, owning the checklist, and **the engineer with the
Netlify account**, who needs only Part 3. Part 3 is written to be handed over
whole.

Everything except Part 3 is already decided and mostly done. This version of
the runbook says what's actually configured, not what to choose — the
original decision points are in git history if a future environment needs
them again. (Historically staged — Stage 1 needed no Google account, Stage 2
added Drive; both are done now, but a couple of checks below still carry
their stage tag.)

---

## 0. Google Drive setup

> ✅ **Decided and done: Setup A** (service account + Shared Drive). The
> account has Shared drives, so this was the available and correct choice —
> the recordings are owned by the organisation, not a named employee, and
> nobody can revoke the grant by leaving or losing their password. §5 has the
> configured state.

### ⚠️ True regardless of setup, and true on every paid tier too

Drive has **no presigned GET**, so audio playback is proxied through the API
rather than served straight from storage — PHI bytes cross the application
server on the way out. And Drive has **no lifecycle rules**, so expired audio
is deleted only by the Celery purge, which runs only while the worker VM is
up. Neither is a free-tier limitation and no plan removes them — worth
knowing before anyone assumes upgrading Google Workspace fixes it.

---

## 1. The stack

| Piece | Service | Cost | Notes |
|---|---|---|---|
| Frontend | **Netlify** | 300 credits/mo free (~15 GB) | Engineer's account. Part 3. |
| API | **Render** web service | 750 instance-hrs/mo free, 0.1 CPU / 512 MB | Sleeps after 15 min idle |
| Worker + Beat | **a purchased always-on VM** | not free — a small VPS | No free host runs a background worker (Render's, Railway's and Fly's free tiers all rule it out). Same shape as the production plan (decision 0036), just smaller. §6. |
| Postgres | **Neon** | 512 MB free, scale-to-zero | Demo-scale only — §8 |
| Redis | **Upstash** | 500 K commands/mo free | ⚠️ Must raise the poll timeout |
| Audio | **Google Drive** | org pooled | §0 and §5 |
| ASR + notes | **Groq** | free tier | Cannot carry a full consult |

---

## 2. Code and Drive integration status

> ✅ Code is not blocking anything here. `STORAGE_BACKEND=drive` is what
> selects the Drive backend on Render, already set (§6). The Drive adapter's
> real behaviour — a browser uploading straight to a live session URI,
> `Range` through the playback proxy, and deletion actually deleting rather
> than trashing — has been verified directly against the real, running Drive
> setup, not just against the adapter's own stubbed tests.

One item is still open, honestly:

- [ ] **A resumed upload skips the right chunks.** Not fully closed — this
      needs a genuinely interrupted multi-part upload to test end to end, and
      every real recording so far has fit in one part. What *is* verified:
      the exact mechanism a resume depends on (`list_uploaded_parts`
      correctly reporting what Drive already has) had a real bug — it
      silently reported "nothing uploaded" for a file Drive had already
      fully received, which surfaced as a recording stuck looping forever
      between "uploading" and "waiting to retry." Found and fixed live,
      confirmed against the actual stuck encounter (self-healed on the very
      next retry, no manual intervention). The mechanism is now proven
      correct; a deliberate kill-the-network-mid-upload test would still be
      the fully conclusive version of this check.

---

## 3. FOR THE ENGINEER — Netlify (hand this section over whole)

You are deploying **only the frontend**: a static Vite/React bundle. The
backend runs elsewhere and you need none of its credentials.

> **In case you notice it elsewhere in this repo:** the Celery worker and
> Beat scheduler run on a purchased always-on VM (§1, §6) — not a free tier,
> and not a spare laptop. None of that is part of what you're deploying here.

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

> ✅ **Status: done.** Neon, Upstash, Groq, the PHI encryption key and the
> JWT secret all exist, and all of them are already set on Render — see §6's
> status note for the full list of variable names.

### Where each value ends up

Nothing from §4 is typed once. The worker runs on a different machine from
the API and gets its configuration from its own shell, so most values are
needed in two or three places — and `PHI_ENCRYPTION_KEY` **must be
byte-identical** in both, or the API writes notes the worker cannot read.

| Value | Render env (§6) | Worker VM (§6) |
|---|:---:|:---:|
| `DATABASE_URL` (Neon) | ✅ | ✅ |
| `REDIS_URL` (Upstash) | ✅ | ✅ |
| `GROQ_API_KEY` | ✅ | ✅ |
| `PHI_ENCRYPTION_KEY` | ✅ | ✅ **same value** |
| `JWT_SECRET` | ✅ | — |
| Drive variables (§5) | ✅ | ✅ |

⚠️ **`apps/api/.env` is not on this list and never will be.** It is the
local-development file. Render reads its own environment-variable settings,
and the worker VM reads whatever is exported in its own shell (or set in
whatever process manager runs it there).

⚠️ **The two-stage order** (get Stage 1 — no Drive — fully working before
turning on Stage 2 — Drive) **is now history, not a live decision**, but
worth remembering for any *future* fresh environment: doing both at once
makes a Drive problem and a Netlify problem indistinguishable on the first
deploy, since they produce similarly vague symptoms.

---

## 5. Google Drive — Setup A, configured

> ✅ **Status: done.** Service account + Shared Drive, matching §0's
> decision. `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` / `GOOGLE_DRIVE_FOLDER_ID` /
> `STORAGE_BACKEND=drive` are already set on Render (§6).

Two facts worth keeping even though the setup itself is done — both are the
kind of trap that fails silently rather than loudly:

- ⚠️ **The Shared Drive membership role matters.** The service account needs
  `Content manager`, not `Contributor`. A Contributor can upload but
  **cannot delete**, so recording works perfectly while consent withdrawal
  and the retention purge quietly remove nothing. If a deletion ever starts
  returning 403, that role is the first thing to check — raise it to
  `Manager` if `Content manager` isn't enough.
- ⚠️ **The audio folder must be inside the Shared Drive, not the account
  root.** A service account has no "My Drive" of its own — an unset or
  wrong folder id has nowhere to write.

The full click-by-click setup steps (creating the Cloud project, the service
account, the JSON key, the Shared Drive itself) are in git history for this
file if a second environment ever needs them from scratch.

---

## 6. Deploy

> ✅ **Status: Render is deployed.** The web service exists and these
> environment variables are set (names only — values live in Render, not
> here):
>
> `AUDIO_RETENTION_DAYS`, `CORS_ALLOW_ORIGINS`, `DATABASE_URL`,
> `ENVIRONMENT`, `GOOGLE_DRIVE_FOLDER_ID`,
> `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`, `GROQ_API_KEY`, `JWT_SECRET`,
> `NOTE_GENERATOR_PROVIDER`, `PHI_ENCRYPTION_KEY`, `REDIS_URL`,
> `REFRESH_COOKIE_SAMESITE`, `REFRESH_COOKIE_SECURE`,
> `S3_PROVISION_BUCKET_ON_STARTUP`, `S3_SECRET_KEY`, `STORAGE_BACKEND`.
>
> Also tried and tested from a local machine against this same deployment —
> Netlify (§3) isn't done yet, so this is what stands in for §7's checks
> until it is.

⚠️ **`S3_SECRET_KEY` needs a real value even though Drive, not S3, is the
storage backend.** Its default (`remedy-dev-secret`, in `.env.example`) is a
fingerprinted published secret, and the boot guard checks it
**unconditionally** — it has no way to know the field is unused. Any
non-default string clears it: `python -c "import secrets;
print(secrets.token_urlsafe(32))"`.

⚠️ **`CORS_ALLOW_ORIGINS` needs the real Netlify URL once §3 exists.** The
guard only refuses `localhost`/`127.0.0.1`, so a placeholder on the reserved
`.example` TLD (the convention `scripts/seed_staging.py` uses) is fine to sit
there in the meantime — but a browser-based login is silently rejected by
CORS, with nothing useful in the API log, until the real domain replaces it.

### Migrations and seeding — reference, since these are one-time/rare

```powershell
cd apps/api
$env:DATABASE_URL = "<neon-url>"
.venv\Scripts\python.exe -m alembic upgrade head
```
```bash
cd apps/api
DATABASE_URL="<neon-url>" .venv/Scripts/python -m alembic upgrade head
```
Deliberate: three processes share this image, and migrating on boot means
three racing for the same lock — always run this by hand, never as part of
Render's own boot.

Seeding (only needed again for a fresh environment, or a fresh demo
account) needs `ENVIRONMENT=staging` — not `production` — or the script
refuses to run against anything that looks real:
```powershell
$env:REMEDY_ALLOW_SYNTHETIC_SEED = "1"
$env:ENVIRONMENT = "staging"
$env:DATABASE_URL = "<neon-url>"
.venv\Scripts\python.exe scripts\seed_staging.py --yes
```

### The worker and Beat, on the purchased VM

The worker needs **no inbound network**, only outbound access to Neon,
Upstash, Drive and Groq.

⚠️ **The worker reads its own environment, entirely separate from Render's.**
It needs every one of §4's values *and* the Drive variables from §5 — miss
one and the worker connects fine, picks up the first queued upload, and then
fails downloading the audio with an error naming whatever this machine's own
local fallback happens to be, not Drive, and not anything that looks like a
missing variable. Found live once already: a worker started with only the
core variables silently fell back to defaults for everything else.

```bash
# On the VM, in the worker's own shell:
cd apps/api
export DATABASE_URL="<neon-url>" REDIS_URL="<upstash-url>" GROQ_API_KEY=…
export PHI_ENCRYPTION_KEY=<the same key as Render>
export STORAGE_BACKEND=drive
export GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON='<paste the whole JSON>'
export GOOGLE_DRIVE_FOLDER_ID=<from §5>

.venv/bin/python -m celery -A app.tasks.celery_app worker --loglevel=info --pool=solo
```

⚠️ **Celery's Redis poll timeout is not optional against Upstash.** Its
default ~1-second blocking read produces about **2,592,000 commands a month
against Upstash's 500,000 — 5× over, with the worker completely idle.** This
is already set in `app/tasks/celery_app.py` (`BROKER_TRANSPORT_OPTIONS`),
not passed as a CLI flag — nothing to add here as long as the deployed code
includes that fix. Confirm periodically: the Upstash dashboard's command
count should grow by dozens an hour, not thousands.

Beat, same shell (env vars already set):
```bash
.venv/bin/python -m celery -A app.tasks.celery_app beat --loglevel=info
```

⚠️ **Exactly one Beat process, ever.** Two double-fire the retention purge,
and **that purge deletes patient data**. Before restarting it, confirm the
old one is actually dead — on the VM, that means checking for a leftover
process, not just launching a new one.

On the VM, both processes should run under something that restarts them on
crash or reboot (a `systemd` service, `pm2`, or equivalent) rather than a
bare terminal session — a VM that reboots for a routine host maintenance
window should not need someone to notice and relaunch these by hand.

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
7. [ ] **Record 20 seconds** and watch the worker log: `transcribe_encounter`
       → `generate_note`, and the file appears in the Drive folder.
8. [ ] **Play a cited passage** → you hear that moment only; playback stops
       at the end of the citation.
9. [ ] **Withdraw consent, then look in Drive** → the file must be gone, and
       **not sitting in Trash**. If it is still there, the service account's
       shared-drive role is too low — see §5.

> **Trying it locally, before Netlify exists:** run the frontend on your own
> machine with its dev server proxying `/api/*` to the real Render backend —
> the same same-origin trick `netlify.toml` performs in production, just
> local. Set `VITE_API_BASE_URL=/` in `apps/web/.env`, then run
> `RENDER_DEV_PROXY_TARGET=https://<render-service>.onrender.com npm run dev`
> from `apps/web`, and open `http://localhost:5173` — checks 4–9 above all
> work the same way. Don't add `localhost` to Render's `CORS_ALLOW_ORIGINS`
> to make this work instead — the production boot guard refuses any
> `localhost` origin on purpose, and will refuse to boot at all if it's
> there.

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
  the worker VM is up.
- **Nothing is alerting.** The alert rules exist; delivery needs a Sentry
  account nobody has created. If the worker dies at 2 a.m., nobody is told.
