"""The Google Drive storage backend (decision 0040).

Drive's resumable protocol differs from S3 multipart in ways that fail
*silently* rather than loudly if the translation is wrong: a 308 read as an
error, a byte offset converted into the wrong part count, a trashed file
reported as deleted. Each of those has a test named after the failure.

No live Drive calls — `httpx` is stubbed. What is verified is the
translation between the two protocols, which is where the bugs live. What
can only be verified against real credentials is listed in decision 0040.
"""

from __future__ import annotations

import pytest

from app.services import storage_drive
from app.services.storage_drive import (
    MIN_PART_SIZE_BYTES,
    DriveError,
    UnsupportedByBackendError,
)


#: Captured at import, before the autouse fixture below replaces it with a
#: stub. The service-account tests need the genuine grant, and once the
#: fixture has patched the module attribute the original is unreachable.
_REAL_ACCESS_TOKEN = storage_drive._access_token


class _Resp:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status: int, *, headers: dict | None = None, payload: dict | None = None, body: bytes = b""):
        self.status_code = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._payload = payload
        self.content = body

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _drive_configured(monkeypatch):
    """Credentials present and a token already cached, so no test needs to
    stub the OAuth round trip unless it is testing the OAuth round trip."""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "google_drive_client_id", "cid", raising=False)
    monkeypatch.setattr(settings, "google_drive_client_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "google_drive_refresh_token", "refresh", raising=False)
    monkeypatch.setattr(settings, "google_drive_folder_id", "folder123", raising=False)
    monkeypatch.setattr(storage_drive, "_access_token", lambda: "test-access-token")
    yield
    storage_drive.reset_token_cache()


# --- the Drive display name -------------------------------------------------


def test_a_real_key_gets_a_clean_hyphenated_name():
    """Real shape, from build_audio_object_key: encounters/{id}/audio/{file}.
    The old naive "/" -> "__" replace produced
    "encounters__enc-1__audio__abc.webm" for this same key -- same
    information, half the noise, requested live after a doctor asked why
    Drive filenames were so hard to read."""
    name = storage_drive._drive_name("encounters/enc-1/audio/abc123.webm")
    assert name == "encounter-enc-1-abc123.webm"


def test_a_full_uuid_encounter_id_round_trips_cleanly():
    key = "encounters/dd209d1b-7501-4512-9825-eb6a20f8f476/audio/c769377d79a84ee7adf8d4d68f6e5b4a.webm"
    name = storage_drive._drive_name(key)
    assert name == "encounter-dd209d1b-7501-4512-9825-eb6a20f8f476-c769377d79a84ee7adf8d4d68f6e5b4a.webm"
    # Still greppable by encounter id, the one property the old scheme's
    # own docstring called out as load-bearing.
    assert "dd209d1b-7501-4512-9825-eb6a20f8f476" in name


def test_an_unrecognized_key_shape_falls_back_to_the_naive_replace():
    """Must never raise on a shape it doesn't recognize -- falls back to
    the old behavior instead, which is reversible either way."""
    name = storage_drive._drive_name("something/unexpected")
    assert name == "something__unexpected"


def test_two_different_keys_never_collide_on_the_drive_name():
    """The transform must stay injective for the shapes that matter --
    two distinct object keys must never resolve to the same Drive
    filename, or a lookup by name (_find_file_id) could return the
    wrong file."""
    a = storage_drive._drive_name("encounters/enc-1/audio/x.webm")
    b = storage_drive._drive_name("encounters/enc-2/audio/x.webm")
    assert a != b


# --- the 308 translation --------------------------------------------------


def test_a_308_reports_the_parts_drive_actually_holds(monkeypatch):
    """`Range: bytes=0-N` is inclusive, so N+1 bytes are stored. Off by one
    here means the client either re-sends a part needlessly or, far worse,
    skips one Drive never received.
    """
    stored = 3 * MIN_PART_SIZE_BYTES
    monkeypatch.setattr(
        storage_drive.httpx,
        "put",
        lambda *a, **kw: _Resp(308, headers={"Range": f"bytes=0-{stored - 1}"}),
    )

    parts = storage_drive.list_uploaded_parts("k", "https://session")

    assert [p["part_number"] for p in parts] == [1, 2, 3]


def test_a_partially_received_part_is_not_reported_as_done(monkeypatch):
    """The floor is deliberate. Re-sending a chunk Drive already has is
    harmless; skipping one it only half-received is a corrupt recording.
    """
    stored = 2 * MIN_PART_SIZE_BYTES + 1024  # two whole parts and a fragment
    monkeypatch.setattr(
        storage_drive.httpx,
        "put",
        lambda *a, **kw: _Resp(308, headers={"Range": f"bytes=0-{stored - 1}"}),
    )

    parts = storage_drive.list_uploaded_parts("k", "https://session")

    assert [p["part_number"] for p in parts] == [1, 2]


def test_a_308_with_no_range_header_means_nothing_stored_yet(monkeypatch):
    monkeypatch.setattr(storage_drive.httpx, "put", lambda *a, **kw: _Resp(308))

    assert storage_drive.list_uploaded_parts("k", "https://session") == []


def test_a_completed_upload_reports_every_part(monkeypatch):
    """A 200 means the whole file landed — a resuming client is catching up
    on an upload that already finished, and must skip everything.

    The payload shape here is the *real* one, captured live against the
    actual Drive API: `kind, id, name, mimeType, teamDriveId, driveId` — no
    `size`, because this endpoint's default field set never includes it. A
    prior version of this function read `size` from this exact response to
    floor/ceiling-divide into a part count, which silently evaluated to 0
    every time (`{}.get("size", 0)` on a body with no such key), so a file
    Drive had already finished receiving kept reporting "nothing uploaded
    yet" forever. Found live: a real recording looping between "uploading"
    and "waiting to retry" with its bytes already sitting in Drive the
    entire time. A test built around a mocked payload that *did* include
    `size` passed the whole time and never caught it — this is why the
    fixture below is the real shape, not a convenient one.
    """
    monkeypatch.setattr(
        storage_drive.httpx,
        "put",
        lambda *a, **kw: _Resp(
            200,
            payload={
                "kind": "drive#file",
                "id": "1ObSuMfTjPnxIRo17jMDgyQrCFC4O1Cy7",
                "name": "encounter-e1-x.webm",
                "mimeType": "audio/webm",
                "teamDriveId": "0AE6CuSS_4eH9Uk9PVA",
                "driveId": "0AE6CuSS_4eH9Uk9PVA",
            },
        ),
    )

    parts = storage_drive.list_uploaded_parts("k", "https://session")

    # Any part number a real plan could ever ask about must be covered —
    # `uploader.ts`'s loop only checks membership for its own plan's part
    # numbers, never the exact list length, so over-covering is harmless
    # and under-covering is the entire bug this guards against.
    reported = {p["part_number"] for p in parts}
    assert {1, 2, 3, 4}.issubset(reported)


def test_an_expired_session_reports_no_parts_rather_than_raising(monkeypatch):
    """A 404 means the session is gone. Nothing survives it, and the honest
    answer is an empty list so the client starts over — not an exception
    that would dead-letter the encounter.
    """
    monkeypatch.setattr(storage_drive.httpx, "put", lambda *a, **kw: _Resp(404))

    assert storage_drive.list_uploaded_parts("k", "https://session") == []


# --- the session URI ------------------------------------------------------


def test_opening_a_session_returns_the_location_header(monkeypatch):
    captured = {}

    def _post(url, **kw):
        captured.update(url=url, headers=kw.get("headers"), params=kw.get("params"), json=kw.get("json"))
        return _Resp(200, headers={"Location": "https://upload.example/session-abc"})

    monkeypatch.setattr(storage_drive.httpx, "post", _post)

    session = storage_drive.create_multipart_upload("encounters/e1/audio/x.weba", "audio/webm")

    assert session == "https://upload.example/session-abc"
    assert captured["params"]["uploadType"] == "resumable"
    # The folder is applied, or every recording lands in the account root
    # where no human can find it to delete.
    assert captured["json"]["parents"] == ["folder123"]
    # Drive has no folder paths, so the key is flattened into the name —
    # and must stay greppable by encounter id (see _drive_name's own tests
    # for the exact scheme this flattens into).
    assert "encounter-e1-" in captured["json"]["name"]


def test_a_session_without_a_location_header_is_an_error(monkeypatch):
    monkeypatch.setattr(storage_drive.httpx, "post", lambda url, **kw: _Resp(200))

    with pytest.raises(DriveError, match="no session URI"):
        storage_drive.create_multipart_upload("k", "audio/webm")


def test_every_part_gets_the_same_uri_and_that_is_correct(monkeypatch):
    """Drive has one upload target per file; chunks are distinguished by
    `Content-Range`, not by URL. Returning the same URI is the protocol,
    not a shortcut.
    """
    session = "https://upload.example/session-abc"

    assert storage_drive.presign_part_upload("k", session, 1) == session
    assert storage_drive.presign_part_upload("k", session, 7) == session


# --- what Drive cannot do -------------------------------------------------


def test_presigned_playback_raises_rather_than_returning_a_dead_url():
    """The single most important test here. Returning a URL that 401s in a
    browser would produce exactly the dead play button decision 0030 exists
    to prevent — a failure the doctor sees and cannot explain.
    """
    with pytest.raises(UnsupportedByBackendError, match="no presigned download URL"):
        storage_drive.presign_audio_playback("encounters/e1/audio/x.weba")


def test_the_part_size_matches_drives_requirement_not_s3s():
    """256 KiB, not 5 MiB. The client reads this from `POST /upload/init`,
    so a wrong value here produces chunks Drive rejects.
    """
    assert MIN_PART_SIZE_BYTES == 256 * 1024
    assert MIN_PART_SIZE_BYTES % (256 * 1024) == 0


# --- reads ----------------------------------------------------------------


def _stub_lookup(monkeypatch, file_id: str | None):
    def _get(url, **kw):
        if url.rstrip("/").endswith("/files"):
            return _Resp(200, payload={"files": [{"id": file_id}] if file_id else []})
        return _Resp(200, payload={"id": file_id, "size": "1024", "mimeType": "audio/webm"})

    monkeypatch.setattr(storage_drive.httpx, "get", _get)


def test_head_object_returns_none_for_a_missing_file(monkeypatch):
    """Phase 3 verifies audio against storage rather than trusting the
    database, so `None` must mean "absent" and never "we could not check".
    """
    _stub_lookup(monkeypatch, None)

    assert storage_drive.head_object("encounters/e1/audio/gone.weba") is None


def test_head_object_reports_size_for_a_present_file(monkeypatch):
    _stub_lookup(monkeypatch, "file-1")

    result = storage_drive.head_object("encounters/e1/audio/x.weba")

    assert result is not None
    assert result["ContentLength"] == 1024


def test_a_range_request_is_passed_through_for_playback(monkeypatch):
    """The grounding UI plays one cited passage. Without Range every click
    would pull the whole consultation through the API.
    """
    seen = {}

    def _get(url, **kw):
        if url.rstrip("/").endswith("/files"):
            return _Resp(200, payload={"files": [{"id": "file-1"}]})
        seen.update(kw.get("headers") or {})
        return _Resp(
            206,
            headers={"Content-Range": "bytes 1000-1999/45678"},
            body=b"x" * 1000,
        )

    monkeypatch.setattr(storage_drive.httpx, "get", _get)

    body, status, length, total = storage_drive.stream_object_range("k", "bytes=1000-1999")

    assert seen.get("Range") == "bytes=1000-1999"
    assert status == 206
    assert length == 1000
    assert total == 45678  # the browser needs the full size to seek


def test_a_malformed_range_header_is_ignored_rather_than_forwarded(monkeypatch):
    """A client-supplied header goes to a third party, so it is validated
    rather than proxied blindly.
    """
    seen = {}

    def _get(url, **kw):
        if url.rstrip("/").endswith("/files"):
            return _Resp(200, payload={"files": [{"id": "file-1"}]})
        seen.update(kw.get("headers") or {})
        return _Resp(200, body=b"whole file")

    monkeypatch.setattr(storage_drive.httpx, "get", _get)

    storage_drive.stream_object_range("k", "not-a-range; drop table")

    assert "Range" not in seen


# --- deletion -------------------------------------------------------------


def test_delete_uses_files_delete_so_the_audio_is_not_merely_trashed(monkeypatch):
    """`files.delete` bypasses the trash. Trashing would leave a withdrawn
    patient's recording recoverable for 30 days, making both P0-1 and the
    retention purge quietly untrue.
    """
    called = {}

    def _get(url, **kw):
        return _Resp(200, payload={"files": [{"id": "file-1"}]})

    def _delete(url, **kw):
        called["url"] = url
        return _Resp(204)

    monkeypatch.setattr(storage_drive.httpx, "get", _get)
    monkeypatch.setattr(storage_drive.httpx, "delete", _delete)

    assert storage_drive.delete_object("encounters/e1/audio/x.weba") is True
    assert "/files/file-1" in called["url"]
    assert "trash" not in called["url"]


def test_deleting_an_already_absent_file_succeeds(monkeypatch):
    monkeypatch.setattr(storage_drive.httpx, "get", lambda url, **kw: _Resp(200, payload={"files": []}))

    assert storage_drive.delete_object("gone") is True


def test_a_delete_failure_returns_false_rather_than_raising(monkeypatch):
    """A withdrawal must never fail because storage was briefly
    unreachable: the consent ledger entry is the legal record and has to
    persist regardless. Same contract as the S3 backend.
    """
    import httpx as real_httpx

    def _boom(*a, **kw):
        raise real_httpx.ConnectError("network gone")

    monkeypatch.setattr(storage_drive.httpx, "get", _boom)

    assert storage_drive.delete_object("k") is False


# --- errors must not carry content ---------------------------------------


def test_drive_errors_never_quote_a_response_body(monkeypatch):
    """`pipeline._mark_stage_failure` writes `str(exc)[:500]` into
    `Encounter.last_pipeline_error`, an unencrypted column whose safety
    argument is that it holds vendor errors only. A Drive error body can
    echo a file name, and file names here carry the encounter id.
    """
    secret = "encounters__abc123__audio__recording.weba"
    monkeypatch.setattr(
        storage_drive.httpx,
        "post",
        lambda url, **kw: _Resp(403, payload={"error": {"message": f"quota exceeded for {secret}"}}),
    )

    with pytest.raises(DriveError) as exc:
        storage_drive.create_multipart_upload("k", "audio/webm")

    assert secret not in str(exc.value)
    assert "403" in str(exc.value)  # still actionable


def test_missing_credentials_name_the_variable_not_the_value(monkeypatch):
    """A misconfigured deploy should say which variable is missing, not fail
    with an opaque 401 from Google an hour into the pilot.
    """
    import importlib

    from app.core.config import get_settings

    # The autouse fixture stubs _access_token; reload to get the real one.
    real = importlib.reload(storage_drive)
    real.reset_token_cache()
    monkeypatch.setattr(get_settings(), "google_drive_client_id", "cid", raising=False)
    monkeypatch.setattr(get_settings(), "google_drive_client_secret", "secret", raising=False)
    monkeypatch.setattr(get_settings(), "google_drive_refresh_token", "", raising=False)

    with pytest.raises(real.DriveError, match="GOOGLE_DRIVE_REFRESH_TOKEN"):
        real._access_token()


# --- the dispatcher -------------------------------------------------------


def test_the_dispatcher_routes_to_the_configured_backend(monkeypatch):
    from app.core.config import get_settings
    from app.services import storage

    monkeypatch.setattr(get_settings(), "storage_backend", "drive", raising=False)
    assert storage._backend() is storage_drive

    monkeypatch.setattr(get_settings(), "storage_backend", "s3", raising=False)
    from app.services import storage_s3

    assert storage._backend() is storage_s3


# --- shared drives --------------------------------------------------------
#
# A Shared Drive is invisible to the Drive API unless each call opts in, and
# the API does not complain: it answers 200 with an empty result. So these
# assert the query parameters rather than a behaviour, because the behaviour
# they prevent is *silence*.


def _capture_get_params(monkeypatch) -> dict:
    """Run a lookup and hand back the params the module actually sent."""
    seen: dict = {}

    def _get(url, **kwargs):
        seen.update(kwargs.get("params") or {})
        return _Resp(200, payload={"files": []})

    monkeypatch.setattr(storage_drive.httpx, "get", _get)
    return seen


def test_file_lookup_opts_into_shared_drives(monkeypatch):
    """`files.list` needs BOTH flags; every other call needs only the first,
    which is how this one came to be missed. Without them a Shared Drive
    reads as an empty drive.
    """
    seen = _capture_get_params(monkeypatch)
    storage_drive.head_object("encounters/e1/audio/abc.webm")

    assert seen["supportsAllDrives"] == "true"
    assert seen["includeItemsFromAllDrives"] == "true"


def test_a_shared_drive_recording_is_findable_and_therefore_deletable(monkeypatch):
    """The failure this guards is not an error, it is a false success.

    `delete_object` resolves a key through `_find_file_id`, so a lookup that
    cannot see the Shared Drive returns None, which reads as "already gone".
    A consent withdrawal would then report success having deleted nothing —
    P0-1 violated, silently, with the recording still in Drive.
    """
    deleted: list[str] = []

    def _get(url, **kwargs):
        params = kwargs.get("params") or {}
        # Stand in for Drive: a file in a Shared Drive is returned only when
        # the caller opted in, exactly as the real API behaves.
        if params.get("includeItemsFromAllDrives") != "true":
            return _Resp(200, payload={"files": []})
        return _Resp(200, payload={"files": [{"id": "shared-file-1", "size": "2048"}]})

    def _delete(url, **kwargs):
        deleted.append(url)
        return _Resp(204)

    monkeypatch.setattr(storage_drive.httpx, "get", _get)
    monkeypatch.setattr(storage_drive.httpx, "delete", _delete)

    assert storage_drive.delete_object("encounters/e1/audio/abc.webm") is True
    assert len(deleted) == 1
    assert "shared-file-1" in deleted[0]


# --- service-account auth -------------------------------------------------
#
# The grant that takes the human out of the loop. It only works against a
# Shared Drive, because a service account has no storage quota of its own.


def _service_account_json() -> str:
    """A real, throwaway RSA key — the signing path is the thing under test,
    so a fake key would test nothing."""
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return json.dumps({"client_email": "remedy@project.iam.gserviceaccount.com", "private_key": pem})


def test_a_service_account_is_preferred_over_a_humans_refresh_token(monkeypatch):
    """Precedence is deliberate: configuring a service account is precisely
    the act of deciding to stop depending on a person's grant, so it must win
    even while the older refresh token is still sitting in the environment.
    """
    import base64
    import json

    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "google_drive_service_account_json", _service_account_json(), raising=False)
    storage_drive.reset_token_cache()

    sent: dict = {}

    def _post(url, **kwargs):
        sent.update(kwargs.get("data") or {})
        return _Resp(200, payload={"access_token": "sa-token", "expires_in": 3600})

    monkeypatch.setattr(storage_drive.httpx, "post", _post)

    assert _REAL_ACCESS_TOKEN() == "sa-token"
    assert sent["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    assert "refresh_token" not in sent

    # The assertion is a real JWT: three parts, and claims Google will accept.
    header_b64, claims_b64, signature_b64 = sent["assertion"].split(".")
    claims = json.loads(base64.urlsafe_b64decode(claims_b64 + "=="))
    assert claims["iss"] == "remedy@project.iam.gserviceaccount.com"
    assert claims["aud"] == storage_drive.GOOGLE_TOKEN_ENDPOINT
    assert claims["scope"] == "https://www.googleapis.com/auth/drive"
    assert claims["exp"] > claims["iat"]
    assert signature_b64 and "=" not in signature_b64  # base64url, unpadded


def test_a_broken_service_account_key_never_reaches_the_error_message(monkeypatch):
    """`_mark_stage_failure` writes `str(exc)` into an unencrypted column, and
    this payload is a *private key*. Same rule as GroqNoteParseError: report
    the shape of the problem, never the material.
    """
    from app.core.config import get_settings

    secret = "-----BEGIN PRIVATE KEY-----SUPERSECRETKEYMATERIAL-----END PRIVATE KEY-----"
    monkeypatch.setattr(
        get_settings(),
        "google_drive_service_account_json",
        '{"client_email": "a@b.iam.gserviceaccount.com", "private_key": "%s"}' % secret,
        raising=False,
    )
    storage_drive.reset_token_cache()

    with pytest.raises(storage_drive.DriveError) as caught:
        _REAL_ACCESS_TOKEN()

    assert "SUPERSECRETKEYMATERIAL" not in str(caught.value)
    assert "private key" in str(caught.value).lower()


def test_malformed_service_account_json_is_named_without_being_quoted(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "google_drive_service_account_json", "not json {", raising=False)
    storage_drive.reset_token_cache()

    with pytest.raises(storage_drive.DriveError) as caught:
        _REAL_ACCESS_TOKEN()

    assert "not json {" not in str(caught.value)
    assert "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON" in str(caught.value)


def test_a_token_endpoint_failure_reports_status_only(monkeypatch):
    """A token-endpoint error body can echo the assertion back."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "google_drive_service_account_json", _service_account_json(), raising=False)
    storage_drive.reset_token_cache()
    monkeypatch.setattr(
        storage_drive.httpx,
        "post",
        lambda url, **kw: _Resp(401, payload={"error": "invalid_grant", "assertion": "eyJhbG..."}),
    )

    with pytest.raises(storage_drive.DriveError) as caught:
        _REAL_ACCESS_TOKEN()

    assert "401" in str(caught.value)
    assert "eyJhbG" not in str(caught.value)


def test_with_no_credentials_at_all_the_error_names_both_options(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "google_drive_service_account_json", "", raising=False)
    monkeypatch.setattr(settings, "google_drive_refresh_token", "", raising=False)
    storage_drive.reset_token_cache()

    with pytest.raises(storage_drive.DriveError) as caught:
        _REAL_ACCESS_TOKEN()

    assert "GOOGLE_DRIVE_REFRESH_TOKEN" in str(caught.value)
    assert "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON" in str(caught.value)
