"""Google Drive as the audio store (decision 0040).

Implements the same interface as `storage_s3.py` over a genuinely
different protocol. Uses plain `httpx` rather than `google-api-python-client`
for the same reason `asr/groq_whisper.py` does: the four REST calls needed
here are simpler than the SDK that would wrap them, and it adds no
dependency.

## What is preserved, and what is lost

**Preserved — the browser still uploads straight to storage.** Drive's
resumable session URI is PUT-able with no `Authorization` header, so audio
bytes never route through the API on the way in. Google demonstrates this
in its own sample code but never states it in prose, so it was verified
empirically before this module was written: a CORS preflight against
`https://www.googleapis.com/upload/drive/v3/files` reflects an arbitrary
`Origin`, allows `PUT`, and allows the `content-range` request header.

**Lost — presigned playback.** Drive has no presigned GET and no way to
override response headers, so `presign_audio_playback` raises rather than
returning something that would not work. Playback is proxied through the
API (`GET /encounters/{id}/audio`), which means PHI bytes cross the
application server on the way out — a property the S3 path existed to
avoid. Stated in decision 0040 rather than buried here.

**Weaker — the upload credential.** An S3 presigned PUT lives for minutes
of our choosing. Drive's session URI lives **one week**, not configurable.

**Gone — the storage-layer retention backstop.** Drive has no bucket
lifecycle rules, so decision 0033's "belt and braces" loses its belt: only
the Celery purge deletes expired audio now.

## The protocol difference that shapes everything below

S3 multipart gives one presigned URL *per part*, uploadable in any order,
and completion is an explicit call listing the parts. Drive gives one
session URI for the *whole* file; chunks go to it **sequentially** with a
`Content-Range` header; every chunk but the last answers `308 Resume
Incomplete`; and the upload completes itself when the final byte lands.

So:
- `presign_part_upload` returns the *same* URI every time. That is correct,
  not a bug.
- `complete_multipart_upload` verifies rather than completes.
- `list_uploaded_parts` asks Drive how far it got and converts a byte
  offset back into whole part numbers — see its docstring for why that
  conversion is safe here and would not be in general.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

DRIVE_FILES_ENDPOINT = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/drive/v3/files"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

#: Drive requires every chunk except the last to be a multiple of 256 KiB.
#: This is the analogue of S3's 5 MiB floor and is returned to the client
#: from `POST /upload/init`, so the browser chunks to whatever the
#: configured backend actually needs.
MIN_PART_SIZE_BYTES = 256 * 1024

#: Drive has no part-number concept — parts are a fiction this adapter
#: maintains so the upload routes and the client can stay unchanged. The
#: ceiling is therefore arbitrary; it exists only to bound the route's
#: path-parameter validation. 10,000 x 256 KiB is 2.5 GiB, far beyond any
#: consultation.
MIN_PART_NUMBER = 1
MAX_PART_NUMBER = 10000

#: Drive's own documented resumable-session lifetime. Reported to the
#: client as the URL's expiry so the number it sees is true rather than
#: borrowed from the S3 path's much shorter window.
SESSION_URI_TTL_SECONDS = 7 * 24 * 3600

_RANGE_HEADER = re.compile(r"bytes=(\d+)-(\d*)")
_DRIVE_RANGE = re.compile(r"bytes=0-(\d+)")

_DEFAULT_CONTENT_TYPE = "application/octet-stream"

# A slow-but-real dependency, like the ASR call: generous read timeout,
# fast connect. Contrast storage_s3's short startup-check timeouts.
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=300.0, pool=10.0)


class UnsupportedByBackendError(RuntimeError):
    """This backend cannot do something the S3-shaped interface implies.

    Raised rather than returning a plausible-looking value, because the
    one place it happens (`presign_audio_playback`) would otherwise hand a
    caller a URL that 401s in a browser — a failure that surfaces as a
    dead play button, which is exactly what Phase 3 was built to prevent.
    """


class DriveError(RuntimeError):
    """A Drive API call failed.

    ⚠️ Never interpolate a response body into this message.
    `app/tasks/pipeline.py:_mark_stage_failure` writes `str(exc)[:500]`
    into `Encounter.last_pipeline_error`, an unencrypted column. A Drive
    error body can echo a file name, and file names here contain the
    encounter id — not PHI itself, but the column's safety argument is
    that it holds vendor errors only, and it is cheaper to keep that
    absolutely true than to reason about each case. Same rule as
    `note_generation/groq.py:GroqNoteParseError`.
    """


# --- auth -----------------------------------------------------------------

#: Access tokens last an hour; caching avoids a token round trip per call.
#: A tuple rather than a dataclass because it is written from one place.
_token_cache: tuple[str, float] | None = None


def _access_token() -> str:
    """Return a cached access token, minting one by whichever grant is configured.

    **A service account is preferred when one is set**, because it takes the
    human out of the loop entirely: nobody owns the recordings personally,
    nobody's departure or quota takes them away, and nobody can revoke the
    grant by tidying up their Google account permissions.

    It requires a **Shared Drive** — a service account has no storage quota
    of its own, so the files must be owned by a shared drive it belongs to.
    Where there is no shared drive (a personal account), the only option is a
    human's refresh token, and decision 0040 records what that costs.

    Precedence is deliberate rather than alphabetical: if both are present,
    the service account wins, because the reason to have configured one at
    all is to stop depending on the human's.
    """
    global _token_cache
    settings = get_settings()

    if _token_cache is not None and _token_cache[1] > time.time() + 60:
        return _token_cache[0]

    if settings.google_drive_service_account_json:
        return _service_account_token()

    missing = [
        name
        for name, value in (
            ("GOOGLE_DRIVE_CLIENT_ID", settings.google_drive_client_id),
            ("GOOGLE_DRIVE_CLIENT_SECRET", settings.google_drive_client_secret),
            ("GOOGLE_DRIVE_REFRESH_TOKEN", settings.google_drive_refresh_token),
        )
        if not value
    ]
    if missing:
        raise DriveError(
            f"Drive storage is selected but {', '.join(missing)} is not set "
            "(and GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON is not set either)."
        )

    response = httpx.post(
        GOOGLE_TOKEN_ENDPOINT,
        data={
            "client_id": settings.google_drive_client_id,
            "client_secret": settings.google_drive_client_secret,
            "refresh_token": settings.google_drive_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=_TIMEOUT,
    )
    return _token_from(response, "refresh the Google Drive access token")


def _service_account_token() -> str:
    """Sign a JWT with the service account's key and trade it for a token.

    This is Google's `jwt-bearer` grant. It is implemented here rather than
    pulled in with `google-auth` for the reason the whole module exists: it
    is one signature and one form post, against a dependency tree that
    would otherwise arrive to do exactly that. `cryptography` is already a
    dependency — the PHI encryption uses it — so RS256 costs nothing new.

    ⚠️ **A service account only works against a Shared Drive.** It has no
    storage quota of its own, so `GOOGLE_DRIVE_FOLDER_ID` must name a
    folder inside a shared drive the service account is a *member* of.
    Point it at a personal folder and every write fails with a quota error
    that does not mention any of this.
    """
    import base64
    import json

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    settings = get_settings()
    try:
        key_data = json.loads(settings.google_drive_service_account_json)
    except ValueError as exc:
        # Never echo the payload: it is a private key.
        raise DriveError("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc

    client_email = key_data.get("client_email")
    private_key_pem = key_data.get("private_key")
    if not client_email or not private_key_pem:
        raise DriveError("The service-account JSON is missing client_email or private_key.")

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    now = int(time.time())
    header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64(
        json.dumps(
            {
                "iss": client_email,
                # Full drive scope: the adapter creates, reads and *deletes*
                # files, and drive.file would only see what this client
                # itself created — which breaks retention after any manual
                # tidy-up in the Drive UI.
                "scope": "https://www.googleapis.com/auth/drive",
                "aud": GOOGLE_TOKEN_ENDPOINT,
                "iat": now,
                "exp": now + 3600,
            }
        ).encode()
    )
    signing_input = header + b"." + claims

    try:
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same problem
        raise DriveError("The service-account private key could not be read.") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise DriveError("The service-account key is not an RSA key.")

    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = (signing_input + b"." + b64(signature)).decode()

    response = httpx.post(
        GOOGLE_TOKEN_ENDPOINT,
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        timeout=_TIMEOUT,
    )
    return _token_from(response, "get a Google Drive access token for the service account")


def _token_from(response: httpx.Response, what: str) -> str:
    """Shared tail of both grants: cache the token, and never quote a body.

    A token-endpoint error body can carry the client id or the assertion,
    and `_mark_stage_failure` writes `str(exc)` into an unencrypted column.
    """
    global _token_cache

    if response.status_code != 200:
        raise DriveError(f"Could not {what} (HTTP {response.status_code}).")
    if response.status_code != 200:
        # Status only. A token-endpoint body can contain the client id.
        raise DriveError(f"Could not refresh the Google Drive access token (HTTP {response.status_code}).")

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise DriveError("Google returned no access token.")
    _token_cache = (token, time.time() + float(payload.get("expires_in", 3600)))
    return token


def reset_token_cache() -> None:
    """Drop the cached access token. For tests, and for a credential change
    that should not wait an hour to take effect."""
    global _token_cache
    _token_cache = None


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_access_token()}"}


# --- keys and ids ---------------------------------------------------------
#
# The S3 interface passes an opaque `key` everywhere. Drive addresses files
# by an opaque *file id* it assigns at creation, which we do not know when
# the key is built. So the key stays a path-shaped string (unchanged from
# the S3 path, so `Encounter.audio_object_key` needs no migration and the
# grounding UI's expectations hold) and this module resolves key -> file id
# by searching the configured folder by name.


def _drive_name(key: str) -> str:
    """Drive has no folders-in-a-path concept we want to reproduce, so the
    key becomes a flat file name with separators replaced. Reversible by
    inspection, and it keeps every encounter's audio greppable by id in the
    Drive UI, which matters when a human has to find a file to delete.
    """
    return key.replace("/", "__")


def _find_file_id(key: str) -> str | None:
    settings = get_settings()
    query = f"name = '{_drive_name(key)}' and trashed = false"
    if settings.google_drive_folder_id:
        query += f" and '{settings.google_drive_folder_id}' in parents"

    response = httpx.get(
        DRIVE_FILES_ENDPOINT,
        headers=_auth_headers(),
        params={
            "q": query,
            "fields": "files(id,size,mimeType)",
            "pageSize": 1,
            # ⚠️ `files.list` needs BOTH flags to see a shared drive, and every
            # other call in this module needs only the first — which is exactly
            # why this one was missed. Without them the request still returns
            # 200 with an empty `files` array, so a shared drive looks like an
            # empty drive.
            #
            # That silence is the danger. Five functions resolve a key through
            # here, and the worst is `delete_object`: "no file id" reads as
            # "already gone", so a consent withdrawal would report success
            # while the recording stayed. `head_object` would meanwhile report
            # every encounter's audio as missing, degrading grounding to
            # transcript-only forever.
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        },
        timeout=_TIMEOUT,
    )
    if response.status_code != 200:
        raise DriveError(f"Drive file lookup failed (HTTP {response.status_code}).")
    files = response.json().get("files") or []
    return files[0]["id"] if files else None


# --- upload ---------------------------------------------------------------


def create_multipart_upload(key: str, content_type: str | None) -> str:
    """Open a resumable session and return its URI as the `upload_id`.

    The URI *is* the credential — that is what lets the browser PUT to it
    without our OAuth token. It is stored in `Encounter.audio_upload_id`,
    which is `String(128)`; a Drive session URI is longer than that, so
    decision 0040's migration widens the column. Storing a truncated URI
    would fail at the first chunk with a signature error that names
    nothing.
    """
    settings = get_settings()
    metadata: dict[str, Any] = {"name": _drive_name(key)}
    if settings.google_drive_folder_id:
        metadata["parents"] = [settings.google_drive_folder_id]

    response = httpx.post(
        DRIVE_UPLOAD_ENDPOINT,
        headers={
            **_auth_headers(),
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": content_type or _DEFAULT_CONTENT_TYPE,
        },
        params={"uploadType": "resumable", "supportsAllDrives": "true"},
        json=metadata,
        timeout=_TIMEOUT,
    )
    if response.status_code != 200:
        raise DriveError(f"Could not open a Drive upload session (HTTP {response.status_code}).")

    session_uri = response.headers.get("location")
    if not session_uri:
        raise DriveError("Drive opened an upload session but returned no session URI.")
    return session_uri


def presign_part_upload(key: str, upload_id: str, part_number: int, expires_in: int | None = None) -> str:
    """Return the session URI — the *same* one for every part.

    Not a shortcut: Drive's resumable protocol has exactly one upload
    target per file, and the client distinguishes chunks with a
    `Content-Range` header rather than a different URL. `part_number` is
    accepted to keep the interface identical and is deliberately unused.
    """
    return upload_id


def list_uploaded_parts(key: str, upload_id: str) -> list[dict[str, Any]]:
    """Ask Drive how many bytes it has, and express that as whole parts.

    Drive answers a zero-length `PUT` with `Content-Range: bytes * /size`
    by returning `308` plus a `Range: bytes=0-N` header — a byte offset,
    not a part list. Converting an offset back into part numbers is only
    sound because chunks here are **sequential and uniformly sized** (every
    part but the last is exactly `MIN_PART_SIZE_BYTES`), which the client
    guarantees. It would be wrong for S3, where parts may be any size and
    arrive out of order — which is exactly why this lives in the backend
    rather than in the route.

    A `200`/`201` means Drive already has the whole file: the upload
    finished and this is a resumed client catching up.
    """
    response = httpx.put(
        upload_id,
        headers={"Content-Range": "bytes */*", "Content-Length": "0"},
        timeout=_TIMEOUT,
    )

    if response.status_code in (200, 201):
        size = int(response.json().get("size", 0) or 0)
        return _parts_for_bytes(size)

    if response.status_code == 308:
        received = response.headers.get("range")
        if not received:
            return []  # session open, nothing stored yet
        match = _DRIVE_RANGE.match(received)
        if not match:
            return []
        # `Range: bytes=0-N` is inclusive, so N+1 bytes are held.
        return _parts_for_bytes(int(match.group(1)) + 1)

    if response.status_code == 404:
        # The session expired or was aborted. No parts survive it.
        return []

    raise DriveError(f"Could not query the Drive upload session (HTTP {response.status_code}).")


def _parts_for_bytes(byte_count: int) -> list[dict[str, Any]]:
    """Whole parts implied by a byte offset.

    Deliberately floors: a partially-received part is *not* reported as
    done, so the client re-sends it. Re-sending a chunk Drive already has
    is harmless; skipping one it does not is a corrupt file.
    """
    whole = byte_count // MIN_PART_SIZE_BYTES
    return [
        {"part_number": i + 1, "size_bytes": MIN_PART_SIZE_BYTES, "etag": ""}
        for i in range(whole)
    ]


def complete_multipart_upload(key: str, upload_id: str) -> dict[str, Any]:
    """Verify rather than complete.

    A Drive resumable upload finishes itself when the last byte arrives —
    there is no completion call. So this checks the file actually exists
    and has bytes, which is the same guarantee `storage_s3`'s completion
    gives, arrived at differently.
    """
    file_id = _find_file_id(key)
    if file_id is None:
        raise DriveError("The upload session did not produce a file in Drive.")

    response = httpx.get(
        f"{DRIVE_FILES_ENDPOINT}/{file_id}",
        headers=_auth_headers(),
        params={"fields": "id,size,mimeType", "supportsAllDrives": "true"},
        timeout=_TIMEOUT,
    )
    if response.status_code != 200:
        raise DriveError(f"Could not verify the uploaded Drive file (HTTP {response.status_code}).")

    payload = response.json()
    if int(payload.get("size", 0) or 0) <= 0:
        raise DriveError("The uploaded Drive file is empty.")
    return {"id": payload["id"], "size": payload.get("size")}


def abort_multipart_upload(key: str, upload_id: str) -> None:
    """Cancel a session. Drive documents `DELETE` on the session URI."""
    try:
        httpx.delete(upload_id, timeout=_TIMEOUT)
    except httpx.HTTPError:
        # Best effort, same posture as the S3 backend: an abandoned session
        # expires on its own, so failing to cancel it early is not worth
        # propagating.
        logger.warning("Could not abort the Drive upload session for %s", key, exc_info=True)


# --- read -----------------------------------------------------------------


def download_object(key: str) -> bytes:
    """Whole-file read, for the ASR step. Server-side, so OAuth is fine."""
    file_id = _find_file_id(key)
    if file_id is None:
        raise DriveError("No Drive file for that key.")

    response = httpx.get(
        f"{DRIVE_FILES_ENDPOINT}/{file_id}",
        headers=_auth_headers(),
        params={"alt": "media", "supportsAllDrives": "true"},
        timeout=_TIMEOUT,
    )
    if response.status_code != 200:
        raise DriveError(f"Could not download the Drive file (HTTP {response.status_code}).")
    return response.content


def stream_object_range(key: str, range_header: str | None) -> tuple[bytes, int, int | None, int | None]:
    """Bytes for the playback proxy, honouring `Range`.

    Drive *does* support Range on download, which is what makes proxied
    playback usable at all: the grounding UI plays one cited passage, and
    without Range every click would pull the whole consultation.

    Returns `(body, status, content_length, total_size)`. The status is
    206 when a range was requested and honoured, so the browser's audio
    element can seek.
    """
    file_id = _find_file_id(key)
    if file_id is None:
        raise DriveError("No Drive file for that key.")

    headers = _auth_headers()
    if range_header and _RANGE_HEADER.match(range_header):
        headers["Range"] = range_header

    response = httpx.get(
        f"{DRIVE_FILES_ENDPOINT}/{file_id}",
        headers=headers,
        params={"alt": "media", "supportsAllDrives": "true"},
        timeout=_TIMEOUT,
    )
    if response.status_code not in (200, 206):
        raise DriveError(f"Could not read the Drive file for playback (HTTP {response.status_code}).")

    total: int | None = None
    if content_range := response.headers.get("content-range"):
        # `bytes 0-1023/45678` — the tail is the full size.
        if "/" in content_range and content_range.rsplit("/", 1)[1].isdigit():
            total = int(content_range.rsplit("/", 1)[1])

    return response.content, response.status_code, len(response.content), total


def head_object(key: str) -> dict[str, Any] | None:
    """Does the object exist? `None` when it does not.

    Phase 3 leans on this: it verifies audio against *storage* rather than
    trusting the database, because a row claiming a recording exists is not
    evidence that it does. That reasoning is backend-independent, so the
    contract here is exactly the S3 one — `None` for absent, a dict for
    present, and an exception only for "we could not check".
    """
    file_id = _find_file_id(key)
    if file_id is None:
        return None

    response = httpx.get(
        f"{DRIVE_FILES_ENDPOINT}/{file_id}",
        headers=_auth_headers(),
        params={"fields": "id,size,mimeType", "supportsAllDrives": "true"},
        timeout=_TIMEOUT,
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise DriveError(f"Could not stat the Drive file (HTTP {response.status_code}).")

    payload = response.json()
    return {"ContentLength": int(payload.get("size", 0) or 0), "ContentType": payload.get("mimeType")}


def presign_audio_playback(key: str, expires_in: int | None = None) -> tuple[str, int]:
    """⚠️ Not implementable on Drive. Raises, on purpose.

    Drive offers no presigned GET. The alternatives are an
    OAuth-authenticated API call (the browser has no token, and giving it
    one would be worse) or sharing with "anyone with the link", which makes
    a consultation recording retrievable by anyone who ever sees the URL,
    permanently.

    Returning a URL that 401s would produce precisely the dead play button
    decision 0030 was written to prevent, so this fails loudly and the
    playback route streams through the API instead.
    """
    raise UnsupportedByBackendError(
        "Google Drive has no presigned download URL; audio playback is proxied through the API instead."
    )


# --- lifecycle ------------------------------------------------------------


def delete_object(key: str) -> bool:
    """Delete permanently, not to the trash.

    `files.delete` bypasses the trash, which is what P0-1 withdrawal and
    the retention purge both mean by "deleted". Trashing would leave the
    audio recoverable for 30 days and quietly make both claims false.

    Returns a bool rather than raising, matching the S3 backend: a
    withdrawal must never fail because storage was briefly unreachable —
    the consent ledger entry is the legal record and has to persist
    regardless.
    """
    try:
        file_id = _find_file_id(key)
        if file_id is None:
            return True  # already gone
        response = httpx.delete(
            f"{DRIVE_FILES_ENDPOINT}/{file_id}",
            headers=_auth_headers(),
            params={"supportsAllDrives": "true"},
            timeout=_TIMEOUT,
        )
        return response.status_code in (200, 204, 404)
    except (httpx.HTTPError, DriveError):
        logger.warning("Could not delete the Drive file for %s", key, exc_info=True)
        return False


def ensure_bucket_configured() -> None:
    """Confirm the folder is reachable. There is nothing to create.

    ⚠️ And nothing to configure, which is the loss worth naming: the S3
    backend installs a lifecycle rule here that expires audio after
    `audio_retention_days` **whether or not this application is running**.
    Drive has no equivalent, so decision 0033's two-layer retention loses
    its storage-layer backstop and only the Celery purge remains. Recorded
    in decision 0040; not fixable from this function.

    Swallow-and-warn like the S3 version: a startup check must not be able
    to stop the API booting.
    """
    settings = get_settings()
    if not settings.google_drive_folder_id:
        logger.warning("GOOGLE_DRIVE_FOLDER_ID is unset; Drive uploads will land in the account root.")
        return
    try:
        response = httpx.get(
            f"{DRIVE_FILES_ENDPOINT}/{settings.google_drive_folder_id}",
            headers=_auth_headers(),
            params={"fields": "id,name", "supportsAllDrives": "true"},
            timeout=_TIMEOUT,
        )
        if response.status_code != 200:
            logger.warning("Drive folder check returned HTTP %s", response.status_code)
    except Exception:  # noqa: BLE001 - startup must not fail over a storage check
        logger.warning("Could not verify the configured Drive folder", exc_info=True)
