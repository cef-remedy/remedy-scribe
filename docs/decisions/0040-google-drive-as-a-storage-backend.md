# 0040 — Google Drive as a storage backend, and the three things it costs

**Phase:** deployment · **Decided by:** user (vendor), implementation (design) · **Date:** 2026-09-01

## The ask

Deploy on free tiers, with **Google Drive** for audio, because the engineer
who owns the deployment account asked for it. Decision 0036 chose one VM
with S3-compatible storage; this supersedes it for the free-tier demo only,
and leaves 0036 standing as the answer for a real pilot.

Drive is not S3-compatible, so this is an adapter rather than a config
change. What follows is what the adapter preserves, what it cannot, and
what was verified rather than assumed.

## What was verified before any code was written

Two facts decided the architecture, and **neither is in Google's prose
documentation** — one is only demonstrated in sample code, the other only
observable over the wire.

**1. A browser can PUT to a resumable session URI with no credentials.**
Google's own upload sample sends `Authorization` when *opening* the session
and sends none when uploading the bytes. So the session URI is itself the
bearer of authority, exactly like an S3 presigned URL — and the property
decision 0013 exists to protect (audio never routes through the API on the
way in) survives.

**2. Google's upload host allows cross-origin PUT.** Undocumented, so it was
tested:

```
Access-Control-Allow-Origin:  https://example.netlify.app
Access-Control-Allow-Methods: DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT
Access-Control-Allow-Headers: content-range
```

An arbitrary origin is reflected, `PUT` is allowed, and `content-range` —
the one request header the protocol needs — is allowed. Had this come back
negative, audio would have had to proxy through the API in **both**
directions and the plan would have changed again. It is a ten-minute test
that de-risked two days of work, which is why it came first.

⚠️ Note what is *absent*: no `Access-Control-Expose-Headers`, so JavaScript
cannot read the `Range` header from a `308`. Resume state therefore flows
through our own `GET /upload/parts` (server-to-server, no CORS) — as it
already did on S3. Anyone "simplifying" that to a client-side read will
find it silently returns nothing.

## The three real costs

### 1. There is no service account, and that is a governance problem

Google, verbatim:

> "Service accounts don't have storage quota and can't own files. Instead,
> they must upload files and folders into shared drives, or use OAuth 2.0
> to upload items on behalf of a human user."

And the fix requires paying:

> "Personal Google Accounts (@gmail.com) cannot create shared drives"

So on a free account the app cannot own its own files. Audio is uploaded
**as a named human**, lands in *their* personal 15 GB, and is **owned by
them**. If that person leaves, fills their quota, or revokes the grant, the
clinic's recordings go with them. Gmail shares the same quota, so filling
Drive with audio also stops that person receiving email.

This is not a technical limitation to work around. It is a fact to state to
whoever owns the data.

### 2. Playback stops being direct, and `no-store` stops being guaranteed

Drive has **no presigned GET**. The only ways to hand a browser the bytes
are an OAuth-authenticated API call (the browser has no token, and giving it
one would be worse) or sharing with "anyone with the link" — which makes a
consultation recording permanently retrievable by anyone who sees the URL.

So `presign_audio_playback` **raises** on this backend rather than returning
something plausible. That is deliberate: a URL that 401s in a browser
produces precisely the dead play button decision 0030 was written to
prevent. `GET /encounters/{id}/audio` streams instead, which means:

- PHI bytes cross the application server on the way out — losing a property
  the S3 path had.
- `Cache-Control: no-store` becomes something the route sets by hand rather
  than something storage guarantees, because Drive has no response-header
  override.
- The proxy still honours `Range`, because the grounding UI plays one cited
  passage and without it every click would pull the whole consultation
  through a 0.1-CPU instance.

### 3. Retention loses its storage-layer backstop

Decision 0033 chose two layers on purpose: a bucket lifecycle rule that
expires audio *whether or not this application is running*, plus a Celery
purge for the derived rows Postgres holds. Drive has no lifecycle rules, so
the belt is gone and only the braces remain — and on this deployment the
Celery purge runs only while someone's always-on machine is on.

Not fixable in code. Recorded so nobody later reads decision 0033 and
believes both layers are present.

## Design: a dispatcher, not a rewrite

`storage.py` became a thin dispatcher; the S3 implementation moved verbatim
to `storage_s3.py`; Drive lives in `storage_drive.py`. Same shape as
`get_asr_provider()` and `get_note_generator()`.

The payoff is that **no call site changed**. Every caller still writes
`storage.head_object(...)`, and the existing tests that monkeypatch
`app.services.storage.head_object` kept working untouched. Only two tests
needed editing — the two that reach past the interface to patch the boto3
client directly, which now lives in `storage_s3`.

`httpx` rather than `google-api-python-client`, for the same reason
`asr/groq_whisper.py` calls Groq with raw `httpx`: the four REST calls
needed here are simpler than the SDK that would wrap them, and it adds no
dependency.

### The protocol translation, and where it can go quietly wrong

S3 multipart gives one presigned URL *per part*, uploadable in any order,
completed by an explicit call. Drive gives **one** session URI for the whole
file, chunks go to it **sequentially** with `Content-Range`, every chunk but
the last answers `308 Resume Incomplete`, and the upload completes itself.

Three consequences, each with a test named after the failure it prevents:

- **`presign_part_upload` returns the same URI every time.** That is the
  protocol, not a bug.
- **`list_uploaded_parts` converts a byte offset into part numbers**, and
  deliberately *floors*: a partially-received part is not reported as done,
  because re-sending a chunk Drive already has is harmless while skipping
  one it never received is a corrupt recording. This conversion is only
  sound because chunks are sequential and uniformly sized — it would be
  wrong for S3, which is exactly why it lives in the backend rather than the
  route.
- **`delete_object` uses `files.delete`, not trash.** Trashing would leave a
  withdrawn patient's recording recoverable for 30 days, making both P0-1
  and the retention purge quietly untrue.

### The client bug this surfaced

The uploader checked `response.ok` after each part. **`308` is not ok**, so
every multi-part Drive upload would have failed at the first chunk with
"Part 1 upload failed (HTTP 308)" — an error that reads like a server fault
and is actually success. Now `response.ok || response.status === 308`.

The client also now reads the part size from `POST /upload/init` instead of
assuming S3's 5 MiB floor (Drive requires multiples of 256 KiB), and sends
`Content-Range`, which S3 ignores. One code path serves both.

### One schema change

`Encounter.audio_upload_id` widened 128 → 512 (migration `c7e8f9a0b1d2`).
It holds whatever the backend calls an upload id; for Drive that is the
entire session URI, which is also the credential. A truncated URI fails at
the *first chunk*, from the browser, with an error naming nothing.

## What is tested, and what is not

**20 tests** cover the protocol translation against a stubbed `httpx` —
the 308 arithmetic, the floor, session handling, the deliberate
`UnsupportedByBackendError`, and that Drive errors never quote a response
body into `Encounter.last_pipeline_error` (an unencrypted column, same rule
as `GroqNoteParseError`).

**Not tested, because it needs real credentials.** Listed in
`docs/runbooks/deploy-free-tier.md` as the first things to check once an
account exists: a live browser PUT to a real session URI, `Range` end to end
through the playback proxy, and a genuine resumed upload.

## What would change my mind

- **Cloudflare R2 or Backblaze B2 instead.** Both are S3-compatible, so both
  are four environment variables and *zero* code — no adapter, no proxy, no
  ownership problem, `no-store` preserved, and R2 has free egress. This was
  raised and Drive was chosen anyway; if that is ever revisited, deleting
  this backend is easier than writing it was.
- **A paid Workspace domain.** A Shared Drive removes the ownership problem
  entirely and lets a service account own the files. It does not restore
  presigned playback — that is a Drive limitation at every tier.
- **Audio growing past a demo.** 15 GB shared with Gmail, against the 7–17
  GB a real pilot holds at steady state. The moment this is more than a
  demo, storage needs revisiting regardless of vendor.
