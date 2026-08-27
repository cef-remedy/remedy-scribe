"""S3/MinIO client (Phase 1.1). `boto3` was a declared dependency used
nowhere before this — every function here wraps exactly the boto3 calls
the upload routes (app/api/routes/uploads.py) need, so those routes
never touch boto3 directly and this module is the one place that knows
about buckets, presigning, and multipart mechanics.

The upload protocol is S3 multipart with presigned part URLs (see
docs/decisions/0013): the device PUTs chunk bytes straight to S3/MinIO
using a URL this module mints, so the API server never sees the audio
bytes themselves — only metadata (object key, upload id, part numbers).

⚠️ A real S3 constraint that will silently break uploads if forgotten:
every part in a multipart upload except the *last* one must be at least
5 MiB. A naive client chunking at, say, 1 MiB per part will upload fine
and then fail at completion time with an opaque `EntityTooSmall` error.
This isn't validated here — the client owns chunk size — but it's worth
knowing before debugging a "completion always fails" report.
"""

from __future__ import annotations

import logging
import uuid
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Recognized recording formats -> file extension. Not an enforced
# allowlist (Phase 2.2 hasn't picked a codec yet — see
# docs/decisions/0003-style open calls; inventing a strict allowlist
# before that decision exists would be the same mistake 0011 warned
# against for EncounterPipelineStatus). Anything unrecognized still
# gets accepted, just with a generic extension.
_CONTENT_TYPE_EXTENSIONS = {
    "audio/aac": ".aac",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/opus": ".opus",
    "audio/ogg": ".ogg",
    "audio/webm": ".weba",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"

# S3-imposed multipart limits (not this app's choice — see the module
# docstring's heads-up).
MIN_PART_NUMBER = 1
MAX_PART_NUMBER = 10000
MIN_PART_SIZE_BYTES = 5 * 1024 * 1024  # every part but the last


@lru_cache
def _client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(
            signature_version="s3v4",
            # MinIO (and most non-AWS S3-compatible stores) don't support
            # virtual-hosted-style bucket addressing (bucket.host/key) —
            # only path style (host/bucket/key). Real AWS S3 accepts
            # path style too, so this is safe for both targets rather
            # than needing a MinIO-vs-AWS branch.
            s3={"addressing_style": "path"},
            # boto3's defaults (60s connect, up to several retries with
            # backoff) turn "object storage is unreachable" into a
            # multi-minute hang on every request — including the
            # startup bucket-configuration check, which otherwise stalls
            # API boot (and, worse, every test that spins up a TestClient)
            # for minutes when nothing is listening on s3_endpoint_url.
            # A synchronous request handler should fail fast, not hang.
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 1},
        ),
    )


def build_audio_object_key(encounter_id: str, content_type: str | None) -> str:
    """Server-generated, never client-supplied (see docs/decisions/0013
    for why: an earlier version of this flow let the client hand the
    server an arbitrary `audio_object_key` string with zero proof it
    pointed at anything real). The encounter id in the path also means a
    directory listing of the bucket groups every object by encounter
    without needing a separate index.
    """
    extension = _CONTENT_TYPE_EXTENSIONS.get(content_type or "", ".audio")
    return f"encounters/{encounter_id}/audio/{uuid.uuid4().hex}{extension}"


def create_multipart_upload(key: str, content_type: str | None) -> str:
    response = _client().create_multipart_upload(
        Bucket=get_settings().s3_bucket,
        Key=key,
        ContentType=content_type or _DEFAULT_CONTENT_TYPE,
    )
    return response["UploadId"]


def presign_part_upload(key: str, upload_id: str, part_number: int, expires_in: int | None = None) -> str:
    settings = get_settings()
    return _client().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=expires_in or settings.s3_presigned_url_expires_seconds,
    )


def list_uploaded_parts(key: str, upload_id: str) -> list[dict[str, Any]]:
    """The per-chunk state a resumed upload diffs against — S3 already
    tracks exactly this for any in-progress multipart upload, so there's
    no separate Postgres table mirroring it here (docs/decisions/0013).
    Paginated defensively; a 20-40 minute consult won't come close to
    S3's 1000-parts-per-page limit, but a generic helper shouldn't assume
    that forever.
    """
    client = _client()
    bucket = get_settings().s3_bucket
    parts: list[dict[str, Any]] = []
    part_number_marker = 0
    while True:
        response = client.list_parts(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            PartNumberMarker=part_number_marker,
        )
        for part in response.get("Parts", []):
            parts.append(
                {
                    "part_number": part["PartNumber"],
                    "size_bytes": part["Size"],
                    "etag": part["ETag"],
                }
            )
        if not response.get("IsTruncated"):
            break
        part_number_marker = response["NextPartNumberMarker"]
    return parts


class NoPartsUploadedError(Exception):
    """Raised by complete_multipart_upload when zero parts have landed —
    S3 itself rejects this, but with a less actionable error message."""


def complete_multipart_upload(key: str, upload_id: str) -> dict[str, Any]:
    """Finalizes the object from whatever parts S3 currently has on
    record for this upload_id — deliberately re-derived via list_parts
    rather than trusting a client-reported list, so a party that never
    saw a part's PUT response can't cause us to finalize with a part
    missing or a wrong ETag.
    """
    parts = list_uploaded_parts(key, upload_id)
    if not parts:
        raise NoPartsUploadedError(f"No parts have been uploaded for {key} (upload {upload_id}).")

    response = _client().complete_multipart_upload(
        Bucket=get_settings().s3_bucket,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={
            "Parts": [
                {"PartNumber": p["part_number"], "ETag": p["etag"]}
                for p in sorted(parts, key=lambda p: p["part_number"])
            ]
        },
    )
    return response


def abort_multipart_upload(key: str, upload_id: str) -> None:
    """Not currently wired to a route (Phase 1.1 doesn't ask for a
    cancel button), but cheap to expose now — the bucket lifecycle rule
    (ensure_bucket_configured) is the automatic backstop for uploads
    that are simply abandoned; this is for a deliberate "start over."
    """
    _client().abort_multipart_upload(Bucket=get_settings().s3_bucket, Key=key, UploadId=upload_id)


def download_object(key: str) -> bytes:
    """Phase 1.3: reads a full audio object into memory to send on to the
    ASR provider. Fine at the size a single consult produces (a 20-40
    minute recording, compressed mono, is comfortably single-digit-to-
    low-double-digit MB) — this is not a candidate for true streaming
    until audio files get much larger than that, which isn't expected
    for this product's use case.
    """
    return _client().get_object(Bucket=get_settings().s3_bucket, Key=key)["Body"].read()


def head_object(key: str) -> dict[str, Any] | None:
    """Used to verify a just-completed object actually exists in the
    bucket before the caller commits to it server-side — cheap defense
    against a completion response that looked fine but wasn't.
    """
    try:
        return _client().head_object(Bucket=get_settings().s3_bucket, Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return None
        raise


def ensure_bucket_configured() -> None:
    """Best-effort, idempotent bucket setup: create the bucket if it
    doesn't exist (mainly for local MinIO dev — docker-compose starts a
    bare server with no bucket), then apply default encryption and a
    lifecycle policy. Called once at API startup (app/main.py).

    Deliberately swallow-and-warn rather than raise: in a real AWS
    deployment, bucket administration (encryption, lifecycle) is often
    owned by infra-as-code with the app's IAM role scoped to object
    read/write only, not bucket policy management — this function
    failing there is expected, not a reason to fail API startup.
    """
    import logging

    logger = logging.getLogger(__name__)
    settings = get_settings()
    client = _client()
    bucket = settings.s3_bucket

    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        try:
            client.create_bucket(Bucket=bucket)
        except ClientError as exc:
            logger.warning("Could not create S3 bucket %s: %s", bucket, exc)
            return

    try:
        # SSE-S3 (AES256): no KMS setup required on AWS. Whether this
        # actually encrypts on MinIO depends on the MinIO server's own
        # KMS configuration — this call succeeding does not by itself
        # guarantee encryption-at-rest against a local MinIO with no
        # KMS backend configured. True on real AWS S3 unconditionally.
        client.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
    except ClientError as exc:
        logger.warning("Could not set default encryption on bucket %s: %s", bucket, exc)

    try:
        client.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        # One rule, not two: verified empirically against a
                        # real MinIO that a rule whose *only* action is
                        # AbortIncompleteMultipartUpload is rejected
                        # ("InvalidRequest ... did not validate against our
                        # published schema") — it has to be paired with an
                        # Expiration (or NoncurrentVersionExpiration) action
                        # in the same rule. Splitting these into two separate
                        # rules, which is the more obvious way to write this,
                        # silently failed and left NO lifecycle policy
                        # configured at all — caught only because this test
                        # asserts the rule actually landed, not just that the
                        # call didn't raise.
                        "ID": "audio-retention-and-orphan-upload-cleanup",
                        "Status": "Enabled",
                        "Filter": {"Prefix": "encounters/"},
                        "Expiration": {"Days": settings.audio_retention_days},
                        # The orphan-upload reaper for the presigned-multipart
                        # protocol: a device that starts an upload and never
                        # finishes (crash, uninstall, a doctor who abandons a
                        # recording) leaves a multipart session that costs
                        # storage until *something* aborts it. S3/MinIO can
                        # do that natively without a custom sweep job.
                        "AbortIncompleteMultipartUpload": {
                            "DaysAfterInitiation": settings.s3_abort_incomplete_upload_after_days
                        },
                    },
                ]
            },
        )
    except ClientError as exc:
        logger.warning("Could not set lifecycle configuration on bucket %s: %s", bucket, exc)


def delete_object(key: str) -> bool:
    """Deletes one object. Returns True if it is gone (including if it was
    already absent), False if the delete failed.

    Phase 2.3 (P0-1: "processing stops and the associated audio is queued
    for deletion without undue delay"). Deliberately returns a bool rather
    than raising: a withdrawal must never fail because S3 was briefly
    unreachable — the consent ledger entry is the legal record and has to
    persist regardless, with the retention clock as the backstop for the
    bytes. See app/services/consent.py:handle_withdrawal.
    """
    try:
        _client().delete_object(Bucket=get_settings().s3_bucket, Key=key)
        return True
    except ClientError:
        logger.warning("Could not delete audio object %s", key, exc_info=True)
        return False
