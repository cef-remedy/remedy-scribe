from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.core.security import (
    create_access_token,
    generate_mfa_secret,
    mfa_provisioning_uri,
    verify_mfa_code,
    verify_password,
)
from app.models.clinician import Clinician
from app.schemas.clinician import (
    LoginRequest,
    LogoutRequest,
    MfaEnrollConfirmOut,
    MfaEnrollConfirmRequest,
    MfaEnrollOut,
    MfaEnrollRequest,
    RefreshRequest,
    RevokeSessionsOut,
    TokenResponse,
)
from app.services.auth_rate_limit import (
    AccountLockedError,
    RateLimitedError,
    check_login_rate_limit,
    record_login_attempt,
)
from app.services.refresh_tokens import (
    RefreshTokenInvalidError,
    issue_refresh_token,
    revoke_all_for_clinician,
    revoke_refresh_token,
    rotate_refresh_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    """P0-8: multi-factor authentication for clinician access — password
    AND a valid TOTP code are both required; either failing alone returns
    the same 401 to avoid leaking which factor was wrong.

    Phase 0.3: also rate-limited per-IP and locked-out per-email (see
    app/services/auth_rate_limit.py), and now issues a refresh token
    alongside the access token (see app/services/refresh_tokens.py) —
    the fix for "a doctor gets logged out mid-consultation every 15
    minutes with no way back in short of re-entering a password and MFA
    code."
    """
    ip_address = _client_ip(request)

    try:
        check_login_rate_limit(db, email=payload.email, ip_address=ip_address)
    except RateLimitedError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except AccountLockedError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

    clinician = db.query(Clinician).filter(Clinician.email == payload.email).one_or_none()

    credentials_valid = (
        clinician is not None
        and clinician.is_active
        and verify_password(payload.password, clinician.hashed_password)
        and bool(clinician.mfa_secret)
        and verify_mfa_code(clinician.mfa_secret, payload.mfa_code)
    )
    record_login_attempt(db, email=payload.email, ip_address=ip_address, successful=credentials_valid)

    if not credentials_valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials or MFA code")

    access_token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})
    refresh_token, _ = issue_refresh_token(db, clinician.id)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Silent-renewal endpoint the mobile client calls when its access
    token is expired (or about to be) — no password/MFA re-entry, no
    Authorization header. Rotates the refresh token on every use; a
    replayed/stolen refresh token gets caught here, not at login.
    """
    try:
        new_refresh_token, row = rotate_refresh_token(db, payload.refresh_token)
    except RefreshTokenInvalidError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    clinician = db.get(Clinician, row.clinician_id)
    if clinician is None or not clinician.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Clinician not found or inactive")

    access_token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})
    return TokenResponse(access_token=access_token, refresh_token=new_refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: LogoutRequest, db: Session = Depends(get_db)) -> None:
    """Revokes exactly the one refresh token presented — signing out
    this device without touching any other session the clinician has.
    """
    revoke_refresh_token(db, payload.refresh_token)


@router.post("/mfa/enroll", response_model=MfaEnrollOut)
def mfa_enroll(payload: MfaEnrollRequest, db: Session = Depends(get_db)) -> MfaEnrollOut:
    """Step 1 of 2 (Phase 0.3) — replaces "the TOTP secret can only be
    created by a seed script." Only usable on an account with no active
    MFA yet: an already-enrolled clinician re-provisioning here would
    let anyone who has since learned the password silently swap out a
    real MFA secret for their own. Re-enrollment for an already-active
    account is intentionally not self-service (see
    docs/decisions/0007) — it needs an admin/support path, not built
    here.
    """
    clinician = db.query(Clinician).filter(Clinician.email == payload.email).one_or_none()
    if clinician is None or not clinician.is_active or not verify_password(payload.password, clinician.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    if clinician.mfa_secret:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA is already enrolled for this account")

    secret = generate_mfa_secret()
    clinician.mfa_secret_pending = secret
    db.add(clinician)
    db.commit()

    return MfaEnrollOut(provisioning_uri=mfa_provisioning_uri(secret, account_email=clinician.email))


@router.post("/mfa/enroll/confirm", response_model=MfaEnrollConfirmOut)
def mfa_enroll_confirm(payload: MfaEnrollConfirmRequest, db: Session = Depends(get_db)) -> MfaEnrollConfirmOut:
    """Step 2 of 2: one valid code against the *pending* secret moves it
    to `mfa_secret`, the one login actually checks. Until this succeeds,
    /mfa/enroll can be called again (e.g. the QR was mis-scanned) — a
    pending secret is disposable; only the confirmed one matters.
    """
    clinician = db.query(Clinician).filter(Clinician.email == payload.email).one_or_none()
    if clinician is None or not clinician.is_active or not verify_password(payload.password, clinician.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")

    if not clinician.mfa_secret_pending:
        raise HTTPException(status.HTTP_409_CONFLICT, "No pending MFA enrollment for this account")

    if not verify_mfa_code(clinician.mfa_secret_pending, payload.code):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid MFA code")

    clinician.mfa_secret = clinician.mfa_secret_pending
    clinician.mfa_secret_pending = None
    db.add(clinician)
    db.commit()

    return MfaEnrollConfirmOut(enrolled=True)


@router.post("/clinicians/{clinician_id}/revoke-sessions", response_model=RevokeSessionsOut)
def revoke_sessions(
    clinician_id: str,
    db: Session = Depends(get_db),
    # The lost-phone path: an admin revokes on the clinician's behalf,
    # since the clinician themself typically can't reach an authenticated
    # endpoint from the device they no longer have. Every live refresh
    # token dies immediately; any access token already in the wild keeps
    # working until it naturally expires (<= access_token_expire_minutes).
    _actor: Clinician = Depends(require_role("admin")),
) -> RevokeSessionsOut:
    clinician = db.get(Clinician, clinician_id)
    if clinician is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Clinician not found")

    count = revoke_all_for_clinician(db, clinician_id)
    return RevokeSessionsOut(revoked_count=count)
