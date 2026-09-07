# 0041 — Upload parts are cut on exact byte boundaries, not grouped by whole chunks

**Phase:** post-deploy · **Decided by:** implementation (bug found live) · **Date:** 2026-09-07

**Decision:** `planParts` cuts a recording into parts at **exact multiples of
the backend's part size** — every part but the last is precisely `partSize`
bytes — and the upload body is one `Blob` sliced at those offsets, rather
than a regrouping of whole recorder chunks. A part is now allowed to end
mid-chunk; that is required, not incidental.

**What was actually wrong, found live rather than by a test:** a 1:35
consultation sat on "Part 2 upload failed (HTTP 503)" through all 8 retry
attempts — deterministic, not flaky. `planParts` grouped whole ~17 KB
recorder chunks until their running total *first reached* the Drive backend's
256 KiB floor, which satisfies S3's "at least 5 MiB" rule but not Drive's
"exact multiple of 256 KiB" one (decision 0040). Part 1 overshot to 275,072
bytes; Drive — which persists a resumable upload in 256 KiB increments and
is queried by byte offset, not by part list — kept only the aligned
262,144-byte prefix and answered `308`. Part 2 then announced
`Content-Range: bytes 275072-...`, a byte Drive had never reached, and every
retry re-derived the same wrong offset from the same ragged plan. At 32 kbps
mono Opus, 256 KiB is ~65 seconds of audio, so no consultation longer than
about a minute could upload on the Drive backend — which is this
deployment's production storage (decision 0040) — until this fix.

`storage_drive.py` already documented the contract it depends on
(`list_uploaded_parts`'s own docstring: "every part but the last is exactly
`MIN_PART_SIZE_BYTES`, ... which the client guarantees") — the client just
didn't guarantee it. Every multi-part test fixture used 128 KiB chunks,
which divide 256 KiB evenly and so could never produce the ragged part that
broke in the clinic; the fixture was rounder than reality.

**Options considered:** (a) cut on exact byte boundaries, slicing a part
across a chunk seam when needed — as chosen; (b) keep chunk-grouping but pad
the last chunk of an overshooting part to the boundary; (c) round each
recorder chunk up to a multiple of the backend's part size at capture time.

**Why:** (b) invents padding bytes inside a legal-record audio file for a
storage-protocol accounting reason, which is the kind of thing an ASR
transcript should never have to explain. (c) couples the recorder (~5s
chunks, sized for crash tolerance) to whatever part size a *future* storage
backend happens to need, reintroducing exactly the coupling decision 0026
kept out by treating chunks and parts as separate granularities. (a) needs
no invented bytes and keeps the two granularities independent — the plan
only needs the total byte count, not the chunk shape, which is also why
`planParts`'s signature dropped `chunkSizes: number[]` for a single
`totalBytes: number`.

**What would change my mind:** if a future backend's part-size floor were
expressed as a *range* rather than an exact multiple (S3's own "at least,
no exact-multiple requirement" is already that case, and worked correctly
under the old code by coincidence) it would be worth asking whether the
exact-boundary rule should become backend-conditional rather than universal
— today it costs nothing to apply everywhere, since S3 doesn't care where
the cut falls as long as it's ≥5 MiB.
