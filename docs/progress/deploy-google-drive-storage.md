# Deployment enablement — Google Drive as a storage backend

**Date:** 2026-09-03 · **Decision:** [0040](../decisions/0040-google-drive-as-a-storage-backend.md) · **Runbook:** [deploy-free-tier.md](../runbooks/deploy-free-tier.md)

Not a numbered phase. This is the work that stands between the finished
application and it running on the free-tier stack the engineer who owns the
deployment account asked for: **Netlify** for the web app, **Render** for the
API, **Neon** for Postgres, and **Google Drive** for audio.

Drive is not S3-compatible, so it is the only one of those four that needed
code.

## The question that started it

> *"Is the Drive adapter needed to verify the deployment checklist we just made?"*

Mostly no, and that answer is now a table in the runbook rather than a
sentence in a chat window, because it determines the order the engineer
should work in:

| Checklist step | Needs the adapter? |
|---|---|
| Netlify build, SPA rewrite, API proxy | no |
| Render deploy, `$PORT`, production boot guard | no |
| Neon provisioning, migrations, the drift gate | no |
| Login, MFA, worklist, patient search | no |
| Note review, editing, signing | no |
| Grounding **highlights** (click a line, see the passage) | no |
| Recording → upload → transcription | **yes** |
| Audio **playback** (click again, hear the moment) | **yes** |
| Audio retention purge | **yes** |

So the engineer can stand the whole thing up and verify most of it before a
single Google credential exists. That matters more than it sounds: a Drive
misconfiguration and a Netlify misconfiguration produce similarly vague
symptoms, and deploying them together makes the two indistinguishable.

## Ten minutes of testing that decided the architecture

Two facts had to be true for browser-direct upload to survive on Drive, and
**neither is in Google's prose documentation.**

The first is only demonstrated in sample code: their own upload example sends
`Authorization` when *opening* a resumable session and sends none when
uploading the bytes. The session URI is itself the bearer of authority —
structurally the same as an S3 presigned URL, which is what lets decision
0013's property hold (audio never routes through the API on the way in).

The second is not documented anywhere, so it was tested before any code was
written:

```
Access-Control-Allow-Origin:  https://example.netlify.app
Access-Control-Allow-Methods: DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT
Access-Control-Allow-Headers: content-range
```

An arbitrary origin reflected, `PUT` allowed, and `content-range` — the one
request header the protocol needs — allowed. Had that come back negative,
audio would have had to proxy through the API in **both** directions and the
plan would have changed shape again.

⚠️ Note what is *absent*: no `Access-Control-Expose-Headers`. JavaScript
cannot read the `Range` header off a `308`, so resume state keeps flowing
through our own `GET /upload/parts` — server-to-server, no CORS — exactly as
it already did on S3. Anyone later "simplifying" that into a client-side read
will find it silently returns nothing.

## A dispatcher, not a rewrite

`storage.py` became a thin dispatcher, the S3 implementation moved verbatim to
`storage_s3.py` (via `git mv`, so history follows it), and Drive lives in
`storage_drive.py`. Same shape as `get_asr_provider()` and
`get_note_generator()`; `STORAGE_BACKEND=drive` selects it and `s3` stays the
default.

The payoff is that **no call site changed.** Every caller still writes
`storage.head_object(...)`, and the existing tests that monkeypatch
`app.services.storage.head_object` kept working untouched. Only three tests
needed editing — the three that reach *past* the interface to patch the boto3
client, which now lives in `storage_s3`.

`httpx` rather than `google-api-python-client`, for the same reason
`asr/groq_whisper.py` calls Groq with raw `httpx`: four REST calls are simpler
than the SDK that would wrap them, and it adds no dependency.

### Where the protocol translation can go quietly wrong

S3 multipart gives one presigned URL *per part*, uploadable in any order,
completed by an explicit call. Drive gives **one** session URI for the whole
file, chunks go to it **sequentially** with `Content-Range`, every chunk but
the last answers `308 Resume Incomplete`, and the upload completes itself.

Three consequences, each with a test named after the failure it prevents:

- **`presign_part_upload` returns the same URI every time.** That is the
  protocol, not a bug — and it looks enough like a bug to be worth a test.
- **`list_uploaded_parts` converts a byte offset into part numbers, and
  deliberately *floors*.** A partially-received part is not reported as done,
  because re-sending a chunk Drive already has is harmless while skipping one
  it never received is a corrupt recording. The conversion is only sound
  because chunks are sequential and uniformly sized, which is why it lives in
  the backend rather than in the route.
- **`delete_object` uses `files.delete`, not trash.** Trashing would leave a
  withdrawn patient's recording recoverable for 30 days, making both P0-1 and
  the retention purge quietly untrue.

## The client bug this surfaced

The uploader checked `response.ok` after each part. **`308` is not `ok`** — it
is Drive's success for every chunk but the last — so every multi-part Drive
upload would have failed at the first chunk with

```
Part 1 upload failed (HTTP 308)
```

an error that reads like a server fault and is in fact success. The client now
accepts `308`, sends `Content-Range` (which S3 ignores), and reads the part
size from `POST /upload/init` instead of assuming S3's 5 MiB floor, since
Drive requires multiples of 256 KiB. One code path serves both backends.

At 32 kbps a 20–40 minute consultation is 4.6–9.2 MB, so on S3 that is one or
two parts — this bug could well have hidden behind single-part uploads in
casual testing and appeared for the first time on a long consult.

## Three costs, stated rather than absorbed

**There is no service account.** Google: *"Service accounts don't have storage
quota and can't own files"*, and the fix — a shared drive — requires paid
Workspace, since *"Personal Google Accounts (@gmail.com) cannot create shared
drives"*. So audio is uploaded **as a named human** and **owned by them**, out
of *their* 15 GB, which Gmail shares. If that person leaves, fills their quota
or revokes the grant, the clinic's recordings go with them. This is a fact for
whoever owns the data, not a bug to route around.

**Playback stops being direct.** Drive has no presigned GET, so
`presign_audio_playback` **raises `UnsupportedByBackendError`** rather than
returning a URL that would 401 in a browser — which is precisely the dead play
button decision 0030 exists to prevent. `read_audio_playback_url` catches it
and hands back `GET /encounters/{id}/audio` instead, which streams through the
API honouring `Range` (the grounding UI plays one cited passage; without it
every click would pull a whole consultation through a 0.1-CPU instance) and
sets `Cache-Control: no-store` by hand, because Drive has no response-header
override. PHI bytes now cross the application server on the way out. The route
runs the **same** `_audio_state` check as the presign path, so the degradation
ladder is backend-independent.

**Retention loses its storage-layer backstop.** Decision 0033 chose two layers
on purpose — a bucket lifecycle rule expiring audio *whether or not this
application runs*, plus the Celery purge. Drive has no lifecycle rules, so the
belt is gone and only the braces remain. Not fixable in code; recorded so
nobody reads 0033 later and believes both layers are present.

## One schema change

`Encounter.audio_upload_id` widened 128 → 512 (migration `c7e8f9a0b1d2`). It
holds whatever the backend calls an upload id; for Drive that is the entire
session URI, which is also the credential. A truncated URI fails at the *first
chunk*, from the browser, with an error naming nothing — so the column was
widened before the backend that needs it could be selected, rather than after
someone spends an afternoon on it. Applied against real Postgres; the column
reads `character varying(512)` and the drift gate reports no drift.

## Verified

**450 API tests passing**, up from 431, with Postgres and MinIO up so nothing
was skipped. **20 new tests** in `test_storage_drive.py` cover the protocol
translation against a stubbed `httpx`: the `308`-to-part-numbers arithmetic
and its deliberate floor, session handling, the raising presign,
`files.delete` rather than trash, and that Drive errors **never quote a
response body** into `Encounter.last_pipeline_error` — an unencrypted column,
the same rule the Groq parse error already follows. `ruff`, `mypy` (71 source
files) and `tsc` clean; 61 web unit tests unchanged.

**Not verified, because it needs real credentials** — and listed as such in
the runbook as the first three things to check once an account exists:

- a live browser `PUT` to a real session URI (the CORS test used a preflight,
  not a real upload)
- `Range` end to end through the playback proxy
- a genuinely resumed upload after an interruption

## What this does not change

S3 remains the default and is untouched, so the local development stack, the
test suite and decision 0036's single-VM answer for a real pilot all behave
exactly as before. Drive is a free-tier demo path, not a replacement — and if
it is ever revisited, **Cloudflare R2 or Backblaze B2 are four environment
variables and zero code**, with no ownership problem, no proxy and `no-store`
preserved. Deleting this backend would be easier than writing it was.
