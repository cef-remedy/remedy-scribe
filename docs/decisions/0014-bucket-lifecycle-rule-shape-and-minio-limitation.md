# 0014 — Lifecycle rule has to combine both actions in one rule; MinIO silently drops the abort action

**Phase:** 1.1 · **Decided by:** implementation (bug found empirically) · **Date:** 2026-08-25

**Decision:** the bucket's retention-expiration and orphan-multipart-abort
lifecycle actions live in **one combined rule**, not two separate ones.
Separately: this MinIO version's inability to actually enforce the abort
action is accepted for local dev, not worked around.

**What was actually wrong, found by testing against a real MinIO rather
than trusting the boto3 call not raising:**
1. The first version had two separate lifecycle rules — one with
   `Expiration` only, one with `AbortIncompleteMultipartUpload` only.
   `put_bucket_lifecycle_configuration` **raised** on the second rule
   (`InvalidRequest: did not validate against our published schema`).
   Caught by `app/main.py`'s startup hook swallowing the exception into a
   log warning — meaning without a test asserting the rule actually
   landed, this would have shipped with *no lifecycle policy applied at
   all*, silently. Fixed by merging both actions into one rule; verified
   empirically that MinIO accepts `Expiration` + `AbortIncompleteMultipartUpload`
   together but rejects `AbortIncompleteMultipartUpload` alone.
2. Even fixed, `GetBucketLifecycleConfiguration` against this MinIO image
   (`RELEASE.2022-12-02`) never echoes the `AbortIncompleteMultipartUpload`
   action back, despite accepting the `PUT` without error — confirmed by
   reading the raw response directly. Whether it's actually *enforced*
   server-side on this MinIO version is therefore unverified.

**Options considered (for #2):** (a) accept the limitation, document it,
assert only what MinIO actually proves (`Expiration`) in the test — as
chosen; (b) pin a newer MinIO image and re-test; (c) drop the orphan-abort
rule from local dev's lifecycle config entirely to avoid asserting
anything about it.

**Why:** (b) is worth doing eventually but shouldn't block 1.1 — the
docker-compose MinIO image and this test's testcontainers image are two
separate version choices that can each be revisited independently. (c)
would mean the *code* no longer attempts the thing 1.1's own checklist
item asks for ("a lifecycle policy... plus... orphan reaper"), just to
make a local-dev limitation invisible — worse than documenting a known
gap. (a) keeps the real ask in the code, is honest in the test about what
is and isn't proven against MinIO, and the config the app sends is
correct S3 API usage that a real AWS bucket (this system's actual
production target) will both accept and enforce.

**What would change my mind:** before relying on this in a real pilot,
verify the abort behavior against either real AWS S3 or a current MinIO
release with an actual multipart upload left to sit past the configured
day count — this decision explicitly does not claim that verification has
happened.
