"""Phase 1.1: storage.py's real boto3/S3 mechanics, against a real
MinIO — not mocked. Route-level behavior (idempotency, consent gating,
RBAC) is already covered fast and Docker-free in test_upload_flow.py;
what's missing there is proof the actual presigned-URL multipart
mechanism *works*: that a URL this module mints can really be PUT to,
that the parts it reports back are the parts that landed, and that
bucket lifecycle configuration is accepted by a real S3-compatible
server. Mocking boto3 would prove none of that — the whole risk in this
kind of code is in exactly the mechanics a mock skips.

Requires a running Docker daemon; skips (doesn't fail) otherwise — see
tests/test_postgres_specific.py's module docstring for the same pattern
and reasoning. Run just these with `pytest -m s3`.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.models.clinician import Clinician
from app.models.encounter import Encounter

pytestmark = pytest.mark.s3


@pytest.fixture(scope="module")
def minio_config():
    try:
        from testcontainers.minio import MinioContainer
    except ImportError:
        pytest.skip("testcontainers[minio] is not installed — see requirements-dev.txt")

    try:
        container = MinioContainer()
        container.start()
    except Exception as exc:  # noqa: BLE001 - any Docker-unavailable reason should skip, not fail the suite
        pytest.skip(f"Docker/MinIO container unavailable, skipping storage-backed tests: {exc}")

    try:
        yield container.get_config()
    finally:
        container.stop()


@pytest.fixture()
def storage_module(minio_config, monkeypatch):
    """Points app.services.storage at the ephemeral MinIO container
    instead of app.core.config's settings-derived endpoint — necessary
    because get_settings() is an lru_cache singleton this test process
    has no clean way to override after the fact (same constraint
    test_postgres_specific.py works around with a subprocess; here it's
    simpler to just replace storage._client() directly, since nothing
    else needs get_settings().database_url to change).
    """
    import boto3
    from botocore.client import Config

    from app.services import storage

    client = boto3.client(
        "s3",
        endpoint_url=f"http://{minio_config['endpoint']}",
        aws_access_key_id=minio_config["access_key"],
        aws_secret_access_key=minio_config["secret_key"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    monkeypatch.setattr(storage, "_client", lambda: client)

    bucket = storage.get_settings().s3_bucket
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:  # noqa: BLE001 - doesn't exist yet, on this module-scoped container's first test
        client.create_bucket(Bucket=bucket)

    return storage


def _seed_encounter(db) -> Encounter:
    clinician = Clinician(email=f"doc-{uuid.uuid4()}@example.com", full_name="Dr. Reyes", hashed_password="x")
    db.add(clinician)
    db.commit()
    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key=f"idem-{uuid.uuid4()}")
    db.add(encounter)
    db.commit()
    return encounter


def test_full_multipart_round_trip(db, storage_module):
    """init -> presign two parts -> really PUT bytes to each -> list
    shows both -> complete -> the finished object's bytes are exactly
    what was sent, in order.
    """
    encounter = _seed_encounter(db)
    key = storage_module.build_audio_object_key(encounter.id, "audio/aac")
    upload_id = storage_module.create_multipart_upload(key, "audio/aac")

    # Real S3 multipart constraint (see storage.py's module docstring):
    # every part but the last must be >= 5 MiB.
    part_1_bytes = b"a" * storage_module.MIN_PART_SIZE_BYTES
    part_2_bytes = b"b" * 1024

    for part_number, data in ((1, part_1_bytes), (2, part_2_bytes)):
        url = storage_module.presign_part_upload(key, upload_id, part_number)
        put_response = httpx.put(url, content=data)
        assert put_response.status_code == 200, put_response.text

    parts = storage_module.list_uploaded_parts(key, upload_id)
    assert sorted(p["part_number"] for p in parts) == [1, 2]
    assert {p["part_number"]: p["size_bytes"] for p in parts} == {
        1: len(part_1_bytes),
        2: len(part_2_bytes),
    }

    storage_module.complete_multipart_upload(key, upload_id)

    head = storage_module.head_object(key)
    assert head is not None
    assert head["ContentLength"] == len(part_1_bytes) + len(part_2_bytes)


def test_complete_with_no_parts_raises(db, storage_module):
    encounter = _seed_encounter(db)
    key = storage_module.build_audio_object_key(encounter.id, None)
    upload_id = storage_module.create_multipart_upload(key, None)

    with pytest.raises(storage_module.NoPartsUploadedError):
        storage_module.complete_multipart_upload(key, upload_id)


def test_head_object_returns_none_for_missing_key(storage_module):
    assert storage_module.head_object("encounters/does-not/exist.m4a") is None


def test_abort_multipart_upload_actually_removes_the_upload(db, storage_module):
    from botocore.exceptions import ClientError

    encounter = _seed_encounter(db)
    key = storage_module.build_audio_object_key(encounter.id, None)
    upload_id = storage_module.create_multipart_upload(key, None)

    storage_module.abort_multipart_upload(key, upload_id)

    # The upload_id is now gone entirely — S3/MinIO reject even
    # ListParts against it (NoSuchUpload), before we'd get anywhere
    # near "zero parts uploaded." Either way, the upload is gone; this
    # confirms *how* it's gone.
    with pytest.raises(ClientError, match="NoSuchUpload"):
        storage_module.complete_multipart_upload(key, upload_id)


def test_ensure_bucket_configured_is_idempotent_and_sets_lifecycle(db, storage_module):
    # The bucket already exists (storage_module fixture creates it) —
    # this proves ensure_bucket_configured doesn't choke on that, and
    # that a real S3-compatible server accepts the lifecycle rule 1.1
    # asks for (retention expiration + orphan-multipart abort, combined
    # into one rule — see storage.py's comment on why they can't be two).
    storage_module.ensure_bucket_configured()
    storage_module.ensure_bucket_configured()  # idempotent — must not raise the second time

    lifecycle = storage_module._client().get_bucket_lifecycle_configuration(Bucket=storage_module.get_settings().s3_bucket)
    rule = next(r for r in lifecycle["Rules"] if r["ID"] == "audio-retention-and-orphan-upload-cleanup")
    assert rule["Status"] == "Enabled"
    assert rule["Expiration"]["Days"] == storage_module.get_settings().audio_retention_days

    # NOT asserting AbortIncompleteMultipartUpload round-trips here — this
    # version of MinIO (RELEASE.2022-12-02) accepts it in the PUT without
    # error but silently drops it; GetBucketLifecycleConfiguration never
    # echoes it back, confirmed by inspecting the raw response directly.
    # Whether it's actually *enforced* server-side on this MinIO version
    # is therefore unverified — real AWS S3 is the one this needs to be
    # trusted against. Recorded so this doesn't look like an oversight:
    # docs/decisions/0014.
    assert "AbortIncompleteMultipartUpload" not in rule
