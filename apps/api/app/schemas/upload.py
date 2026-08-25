from pydantic import BaseModel


class UploadInitRequest(BaseModel):
    """`content_type` is optional and not enforced against an allowlist
    — Phase 2.2 hasn't picked a recording codec yet, so this schema
    doesn't get to invent one. It's used only to pick a file extension
    and set the S3 object's Content-Type metadata.
    """

    content_type: str | None = None


class UploadInitResponse(BaseModel):
    object_key: str
    upload_id: str
    min_part_size_bytes: int
    max_part_number: int


class PartUploadUrlResponse(BaseModel):
    part_number: int
    url: str
    expires_in_seconds: int


class UploadedPart(BaseModel):
    part_number: int
    size_bytes: int
    etag: str


class UploadPartsStatusResponse(BaseModel):
    """What a resuming client diffs its own local chunk manifest
    against to know which parts it can skip re-uploading."""

    parts: list[UploadedPart]
