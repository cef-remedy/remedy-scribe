# 0013 — Presigned S3 multipart upload: protocol, key ownership, and per-chunk state

**Phase:** 1.1 · **Decided by:** user (protocol) + implementation (the rest) · **Date:** 2026-08-25

**Decision:** the upload protocol is **S3 multipart with presigned part
URLs** (the user's explicit call, from the checklist's three options).
Three implementation decisions followed from building it out:

1. **The server generates `audio_object_key`; the client never supplies
   one.** The old `confirm_upload` endpoint took a client-supplied
   `audio_object_key` string on faith, with no proof it pointed at
   anything real. `POST /upload/init` now generates the key itself
   (`encounters/{id}/audio/{uuid}{ext}`) and returns it.
2. **No Postgres table mirrors per-chunk state.** `GET /upload/parts`
   calls S3's own `ListParts` on demand rather than reading a
   `UploadPart` table this app would have to keep in sync with reality.
3. **`upload/complete` re-derives the parts list from `ListParts` at
   completion time**, rather than trusting a client-reported list of
   `{PartNumber, ETag}` pairs — a party that never saw a part's PUT
   response can't cause the server to finalize with a wrong or missing
   part.

**Options considered (2 and 3 together):** (a) query S3 on demand, as
chosen; (b) maintain a Postgres `UploadPart` table, updated by a
client-reported callback per part, as the source of truth for "what's
landed."

**Why:** (b) is a second source of truth for something S3 already
tracks perfectly for the lifetime of an in-progress multipart upload —
exactly the "claims vs. enforces" drift this whole checklist keeps
finding elsewhere (0.2's unattached `require_role`, 0.4's uncontained
`Note.status`). A client-reported `UploadPart` row can be wrong (client
bug, lost message, replay) in a way `ListParts` cannot, since `ListParts`
reads S3's own record of what actually landed. The cost is one extra S3
API call per resume-status check and per completion — negligible next to
a 20-40 minute audio file's actual upload time.

**What would change my mind:** if per-encounter chunk counts grow large
enough that `ListParts`' pagination becomes a real latency concern (not
expected at single-consult audio sizes), or if the product needs
chunk-level metadata `ListParts` doesn't carry (e.g., a client-side
recording timestamp per chunk) — at that point a Postgres table earns
its keep as an *addition*, not a replacement for `ListParts` as the
completion-time source of truth.
