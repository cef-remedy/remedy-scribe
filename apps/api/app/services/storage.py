"""Object storage — the swap point between backends.

Until now this module *was* the S3 implementation. It became a dispatcher
when a second backend arrived (Google Drive, decision 0040), and the S3
code moved verbatim to `storage_s3.py`. Every call site still writes
`storage.head_object(...)`, so nothing outside this file knows which
backend is configured, and the existing tests that monkeypatch
`app.services.storage.head_object` keep working unchanged.

The same shape as `get_asr_provider()` and `get_note_generator()`: one
factory, one `if`, and a new backend is a new module rather than an edit
to every caller.

## The interface is S3-shaped, and that is not neutral

These names — `create_multipart_upload`, `presign_part_upload`,
`list_uploaded_parts` — are S3's vocabulary, because S3 was here first.
Drive implements the same *shape* through a genuinely different protocol
(one resumable session URI, sequential chunks, `308 Resume Incomplete`),
so `storage_drive.py` is a translation rather than a thin wrapper. Where
the translation is lossy, it says so loudly rather than pretending:
`presign_audio_playback` has **no Drive equivalent at all** and raises,
because Drive has no presigned GET. The playback route handles that
explicitly instead of receiving a URL that would not work.

## Why the constants bind at import

`MIN_PART_SIZE_BYTES` and friends differ per backend — S3 requires 5 MiB
parts, Drive requires multiples of 256 KiB — and `app/api/routes/uploads.py`
reads them in a `Path(ge=..., le=...)` declaration, which FastAPI evaluates
at import. So they are resolved once, here, at import. Changing
`STORAGE_BACKEND` therefore requires a restart, which is true of a
deploy-time setting anyway.
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.services import storage_drive, storage_s3

#: Raised when a backend cannot do something the S3 interface implies.
#: Callers that can degrade (the playback route) catch it; callers that
#: cannot should let it surface rather than invent a fallback.
UnsupportedByBackendError = storage_drive.UnsupportedByBackendError

#: Re-exported so `except storage.NoPartsUploadedError` keeps working.
NoPartsUploadedError = storage_s3.NoPartsUploadedError


def _backend():
    """The configured backend module.

    Resolved per call rather than cached, so a test that flips the setting
    does not need to reach into this module's internals. The lookup is a
    dict access against an already-imported module — free.
    """
    return storage_drive if get_settings().storage_backend == "drive" else storage_s3


# Protocol limits, resolved at import — see the module docstring.
_ACTIVE = storage_drive if get_settings().storage_backend == "drive" else storage_s3
MIN_PART_NUMBER: int = _ACTIVE.MIN_PART_NUMBER
MAX_PART_NUMBER: int = _ACTIVE.MAX_PART_NUMBER
MIN_PART_SIZE_BYTES: int = _ACTIVE.MIN_PART_SIZE_BYTES


def build_audio_object_key(encounter_id: str, content_type: str | None) -> str:
    return _backend().build_audio_object_key(encounter_id, content_type)


def create_multipart_upload(key: str, content_type: str | None) -> str:
    return _backend().create_multipart_upload(key, content_type)


def presign_part_upload(key: str, upload_id: str, part_number: int, expires_in: int | None = None) -> str:
    return _backend().presign_part_upload(key, upload_id, part_number, expires_in)


def presign_audio_playback(key: str, expires_in: int | None = None) -> tuple[str, int]:
    """⚠️ Raises `UnsupportedByBackendError` on Drive.

    Not a stub and not a TODO: Drive has no presigned GET, and the only
    ways to hand a browser a Drive file's bytes are an OAuth-authenticated
    API call or sharing the file with "anyone with the link" — the second
    of which would make a consultation recording publicly retrievable by
    anyone holding the URL. The playback route catches this and streams
    through the API instead. See decision 0040.
    """
    return _backend().presign_audio_playback(key, expires_in)


def list_uploaded_parts(key: str, upload_id: str) -> list[dict[str, Any]]:
    return _backend().list_uploaded_parts(key, upload_id)


def complete_multipart_upload(key: str, upload_id: str) -> dict[str, Any]:
    return _backend().complete_multipart_upload(key, upload_id)


def abort_multipart_upload(key: str, upload_id: str) -> None:
    return _backend().abort_multipart_upload(key, upload_id)


def download_object(key: str) -> bytes:
    return _backend().download_object(key)


def stream_object_range(key: str, range_header: str | None) -> tuple[bytes, int, int | None, int | None]:
    """Bytes for a playback request, honouring an HTTP `Range` header.

    Returns `(body, status, content_length, total_size)` where `status` is
    200 or 206. Only the Drive backend needs this — S3 serves ranges
    directly from a presigned URL and never routes audio through the API —
    so the S3 implementation exists to keep the interface total rather than
    because anything calls it.
    """
    return _backend().stream_object_range(key, range_header)


def head_object(key: str) -> dict[str, Any] | None:
    return _backend().head_object(key)


def delete_object(key: str) -> bool:
    return _backend().delete_object(key)


def ensure_bucket_configured() -> None:
    return _backend().ensure_bucket_configured()
