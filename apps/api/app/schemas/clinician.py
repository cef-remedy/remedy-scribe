from pydantic import BaseModel, EmailStr, field_validator


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    # Optional because `settings.require_mfa=False` (demo-stage toggle,
    # app/core/config.py) skips the check entirely — a client built against
    # that mode never has a code to send. When MFA is required, an absent
    # code simply fails verification like any other wrong one; the schema
    # itself does not enforce a mode it can't see from here.
    mfa_code: str | None = None


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


class RegisterRequest(BaseModel):
    """Self-service account creation — did not exist before the free-tier
    demo. Deliberately narrow: no role field. Letting a signup form assign
    its own role would mean anyone could grant themselves `admin` or
    `compliance`, both of which carry real RBAC-gated read access
    (app/api/deps.py); the route hardcodes `role="doctor"`, the one role
    the product's own worklist is built for.

    Appropriate for a demo/pre-pilot stage, not a real clinic: nothing here
    verifies the email address or asks for a PRC license, both of which a
    real pilot would need before treating a self-registered account as a
    licensed clinician (docs/decisions covers this if it becomes real).
    """

    email: EmailStr
    password: str
    full_name: str

    @field_validator("password")
    @classmethod
    def _password_floor(cls, value: str) -> str:
        # A public, unauthenticated route hashing whatever it's given — a
        # floor belongs here even though nothing else in this app has
        # needed one, since seed/admin-created accounts never went through
        # a validator like this at all.
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters.")
        return value


class RegisterOut(BaseModel):
    id: str


class ClinicianOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    role: str

    model_config = {"from_attributes": True}
