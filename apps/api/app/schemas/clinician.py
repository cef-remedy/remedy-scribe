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
    """

    refresh_token: str


class LogoutRequest(BaseModel):
    """Single-session logout: revokes exactly the refresh token
    presented, leaving any other device's session untouched."""

    refresh_token: str


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
