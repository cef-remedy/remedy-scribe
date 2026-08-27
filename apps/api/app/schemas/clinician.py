from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    mfa_code: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    """Phase 0.3: exchanges a still-valid refresh token for a new
    access/refresh pair. Deliberately takes no Authorization header —
    the access token this call is meant to replace may already be
    expired, which is the whole reason to call it.

    Phase 2.1: optional, because the browser client (decision 0024) never
    sees the refresh token — it rides in an httpOnly cookie the route
    reads instead. The field stays for non-browser callers and for the
    Phase 0.3 tests that exercise rotation/reuse-detection directly.
    """

    refresh_token: str | None = None


class LogoutRequest(BaseModel):
    """Single-session logout: revokes exactly the refresh token
    presented, leaving any other device's session untouched. Optional for
    the same reason as RefreshRequest — the cookie is the browser path.
    """

    refresh_token: str | None = None


class RevokeSessionsOut(BaseModel):
    revoked_count: int


class MfaEnrollRequest(BaseModel):
    """Step 1 of enrollment: proves password ownership, then provisions
    a *pending* secret — not yet the one login checks against."""

    email: EmailStr
    password: str


class MfaEnrollOut(BaseModel):
    provisioning_uri: str  # otpauth:// URI — render as a QR code client-side


class MfaEnrollConfirmRequest(BaseModel):
    """Step 2: one valid code against the pending secret activates it."""

    email: EmailStr
    password: str
    code: str


class MfaEnrollConfirmOut(BaseModel):
    enrolled: bool


class ClinicianOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    role: str

    model_config = {"from_attributes": True}
