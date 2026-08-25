"""Phase 1.1: the chunked, resumable upload flow — routes only. These
tests monkeypatch app.services.storage so they run fast and without a
real S3/MinIO endpoint; storage.py's actual boto3 mechanics (presigned
URLs that really work, multipart completion, bucket lifecycle) are
exercised for real against a testcontainers MinIO in
tests/test_storage_specific.py.
"""

import pytest

from app.core.security import create_access_token
from app.models.clinician import Clinician
from app.models.consent import ConsentLedgerEntry
from app.models.encounter import Encounter

_BUCKET_KEY = "encounters/enc-1/audio/abc123.m4a"


def _seed_encounter(db, *, role: str = "doctor") -> tuple[Encounter, Clinician]:
    clinician = Clinician(email="doc@example.com", full_name="Dr. Reyes", hashed_password="x", role=role)
    db.add(clinician)
    db.commit()
    db.refresh(clinician)

    encounter = Encounter(clinician_id=clinician.id, upload_idempotency_key="idem-upload-1")
    db.add(encounter)
    db.commit()
    db.refresh(encounter)
    return encounter, clinician


def _token(clinician: Clinician) -> str:
    return create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})


def _give_consent(db, encounter: Encounter) -> None:
    db.add(
        ConsentLedgerEntry(
            encounter_id=encounter.id,
            event="given",
            participant_roster="[]",
            purposes="[]",
            script_language="en",
        )
    )
    db.commit()


def _auth(clinician: Clinician) -> dict:
    return {"Authorization": f"Bearer {_token(clinician)}"}


# --- init -------------------------------------------------------------------


def test_init_creates_a_multipart_upload(db, client, monkeypatch):
    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _BUCKET_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", lambda key, ct: "upload-abc")

    encounter, clinician = _seed_encounter(db)

    response = client.post(
        f"/api/v1/encounters/{encounter.id}/upload/init",
        json={"content_type": "audio/aac"},
        headers=_auth(clinician),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object_key"] == _BUCKET_KEY
    assert body["upload_id"] == "upload-abc"
    db.refresh(encounter)
    assert encounter.audio_object_key == _BUCKET_KEY
    assert encounter.audio_upload_id == "upload-abc"


def test_init_is_idempotent_on_retry(db, client, monkeypatch):
    calls = {"n": 0}

    def _fake_create(key, ct):
        calls["n"] += 1
        return "upload-abc"

    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _BUCKET_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", _fake_create)

    encounter, clinician = _seed_encounter(db)

    first = client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))
    second = client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert calls["n"] == 1  # the retry never touched S3 again


def test_init_rejects_once_already_uploaded(db, client, monkeypatch):
    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _BUCKET_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", lambda key, ct: "upload-abc")
    monkeypatch.setattr("app.services.storage.complete_multipart_upload", lambda key, upload_id: {})
    monkeypatch.setattr("app.services.storage.head_object", lambda key: {"ContentLength": 1})
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", lambda encounter_id: None)

    encounter, clinician = _seed_encounter(db)
    _give_consent(db, encounter)
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))
    client.post(f"/api/v1/encounters/{encounter.id}/upload/complete", headers=_auth(clinician))

    response = client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    assert response.status_code == 409


def test_init_returns_404_for_unknown_encounter(db, client):
    _encounter, clinician = _seed_encounter(db)
    response = client.post("/api/v1/encounters/does-not-exist/upload/init", json={}, headers=_auth(clinician))
    assert response.status_code == 404


def test_compliance_cannot_init_upload(db, client):
    encounter, _doctor = _seed_encounter(db)
    compliance = Clinician(email="c@example.com", full_name="C", hashed_password="x", role="compliance")
    db.add(compliance)
    db.commit()

    response = client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(compliance))

    assert response.status_code == 403


# --- part URLs ----------------------------------------------------------


def test_get_part_url_requires_an_init_first(db, client):
    encounter, clinician = _seed_encounter(db)
    response = client.post(f"/api/v1/encounters/{encounter.id}/upload/parts/1", headers=_auth(clinician))
    assert response.status_code == 409


def test_get_part_url_returns_a_presigned_url(db, client, monkeypatch):
    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _BUCKET_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", lambda key, ct: "upload-abc")
    monkeypatch.setattr(
        "app.services.storage.presign_part_upload",
        lambda key, upload_id, part_number, expires_in=None: f"https://s3.example/{key}?part={part_number}",
    )

    encounter, clinician = _seed_encounter(db)
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    response = client.post(f"/api/v1/encounters/{encounter.id}/upload/parts/3", headers=_auth(clinician))

    assert response.status_code == 200
    body = response.json()
    assert body["part_number"] == 3
    assert "part=3" in body["url"]


@pytest.mark.parametrize("part_number", [0, 10001])
def test_part_number_out_of_range_is_rejected(db, client, part_number):
    encounter, clinician = _seed_encounter(db)
    response = client.post(f"/api/v1/encounters/{encounter.id}/upload/parts/{part_number}", headers=_auth(clinician))
    assert response.status_code == 422


def test_list_parts_reflects_what_storage_reports(db, client, monkeypatch):
    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _BUCKET_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", lambda key, ct: "upload-abc")
    monkeypatch.setattr(
        "app.services.storage.list_uploaded_parts",
        lambda key, upload_id: [{"part_number": 1, "size_bytes": 5_000_000, "etag": '"abc"'}],
    )

    encounter, clinician = _seed_encounter(db)
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    response = client.get(f"/api/v1/encounters/{encounter.id}/upload/parts", headers=_auth(clinician))

    assert response.status_code == 200
    assert response.json()["parts"] == [{"part_number": 1, "size_bytes": 5_000_000, "etag": '"abc"'}]


# --- complete ----------------------------------------------------------


def test_complete_requires_an_init_first(db, client):
    encounter, clinician = _seed_encounter(db)
    _give_consent(db, encounter)
    response = client.post(f"/api/v1/encounters/{encounter.id}/upload/complete", headers=_auth(clinician))
    assert response.status_code == 409


def test_complete_rejects_when_no_parts_were_uploaded(db, client, monkeypatch):
    from app.services.storage import NoPartsUploadedError

    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _BUCKET_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", lambda key, ct: "upload-abc")

    def _raise(key, upload_id):
        raise NoPartsUploadedError("no parts")

    monkeypatch.setattr("app.services.storage.complete_multipart_upload", _raise)

    encounter, clinician = _seed_encounter(db)
    _give_consent(db, encounter)
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    response = client.post(f"/api/v1/encounters/{encounter.id}/upload/complete", headers=_auth(clinician))

    assert response.status_code == 409


def test_complete_is_idempotent_on_retry(db, client, monkeypatch):
    calls = {"n": 0}

    def _fake_complete(key, upload_id):
        calls["n"] += 1
        return {}

    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _BUCKET_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", lambda key, ct: "upload-abc")
    monkeypatch.setattr("app.services.storage.complete_multipart_upload", _fake_complete)
    monkeypatch.setattr("app.services.storage.head_object", lambda key: {"ContentLength": 1})
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", lambda encounter_id: None)

    encounter, clinician = _seed_encounter(db)
    _give_consent(db, encounter)
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    first = client.post(f"/api/v1/encounters/{encounter.id}/upload/complete", headers=_auth(clinician))
    second = client.post(f"/api/v1/encounters/{encounter.id}/upload/complete", headers=_auth(clinician))

    assert first.status_code == second.status_code == 200
    assert calls["n"] == 1  # the retry never re-called S3's complete_multipart_upload
    db.refresh(encounter)
    assert encounter.pipeline_status == "uploaded"


def test_complete_sets_retention_and_kicks_pipeline(db, client, monkeypatch):
    pipeline_calls = []

    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _BUCKET_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", lambda key, ct: "upload-abc")
    monkeypatch.setattr("app.services.storage.complete_multipart_upload", lambda key, upload_id: {})
    monkeypatch.setattr("app.services.storage.head_object", lambda key: {"ContentLength": 1})
    monkeypatch.setattr("app.tasks.pipeline.run_pipeline", pipeline_calls.append)

    encounter, clinician = _seed_encounter(db)
    _give_consent(db, encounter)
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    response = client.post(f"/api/v1/encounters/{encounter.id}/upload/complete", headers=_auth(clinician))

    assert response.status_code == 200
    assert response.json()["audio_retention_expires_at"] is not None
    assert pipeline_calls == [encounter.id]


def test_complete_502s_when_the_finalized_object_cannot_be_found(db, client, monkeypatch):
    monkeypatch.setattr("app.services.storage.build_audio_object_key", lambda encounter_id, ct: _BUCKET_KEY)
    monkeypatch.setattr("app.services.storage.create_multipart_upload", lambda key, ct: "upload-abc")
    monkeypatch.setattr("app.services.storage.complete_multipart_upload", lambda key, upload_id: {})
    monkeypatch.setattr("app.services.storage.head_object", lambda key: None)  # "completed" but not actually there

    encounter, clinician = _seed_encounter(db)
    _give_consent(db, encounter)
    client.post(f"/api/v1/encounters/{encounter.id}/upload/init", json={}, headers=_auth(clinician))

    response = client.post(f"/api/v1/encounters/{encounter.id}/upload/complete", headers=_auth(clinician))

    assert response.status_code == 502
    db.refresh(encounter)
    assert encounter.pipeline_status == "recording"  # not advanced on a failed verification
