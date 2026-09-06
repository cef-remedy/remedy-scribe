from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    generate_mfa_secret,
    hash_password,
    mfa_provisioning_uri,
    password_needs_rehash,
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
    RegisterOut,
    RegisterRequest,
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


# --- refresh-token transport (Phase 2.1, decision 0024) -------------------
#
# The refresh token moved from "a JSON field the client stores" to an
# httpOnly cookie. This is the one respect in which the browser client is
# strictly stronger than the retired mobile plan: httpOnly means an XSS
# payload cannot read the token at all, while expo-secure-store was always
# readable by app code. The access token is unchanged — still short-lived
# and held in memory only (decisions 0006/0007).
#
# Precedence: an explicitly-presented body token wins over the cookie, and
# getting that backwards is a real bug rather than a style choice. With
# cookie-first, a caller that names a specific token cannot actually use
# it while any cookie exists — which silently broke Phase 0.3's
# reuse-detection tests: they present a deliberately-stale token and
# expect 401, but the route rotated the (valid) cookie instead and
# returned 200. Worse, single-session logout would revoke whichever
# session the cookie happened to hold rather than the one named.
#
# Body-first costs nothing in the browser: the httpOnly guarantee is that
# XSS cannot *read* the cookie, which precedence does not affect, and a
# cross-origin caller cannot set a JSON body past the CORS allow-list or
# send the SameSite=lax cookie on a cross-site POST anyway.


def _set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        max_age=settings.refresh_token_expire_hours * 3600,
        # Scoped to the auth routes that actually use it: no other endpoint
        # has any business receiving this cookie.
        path="/api/v1/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        path="/api/v1/auth",
    )


def _clear_cookie_headers() -> dict[str, str]:
    """Cookie-clearing headers suitable for an HTTPException.

    Mutating the injected `response` and then raising HTTPException does
    NOT work: FastAPI builds a fresh response for the exception and the
    mutation is silently discarded, so the dead cookie stays in the
    browser and every subsequent silent renewal fails identically. The
    fix is to carry the header on the exception itself — serialized by
    Starlette's own set_cookie/delete_cookie rather than hand-rolled,
    since Set-Cookie attribute formatting is easy to get subtly wrong.
    """
    scratch = Response()
    _clear_refresh_cookie(scratch)
    return {"set-cookie": scratch.headers["set-cookie"]}


def _read_refresh_token(request: Request, body_token: str | None) -> str:
    token = body_token or request.cookies.get(get_settings().refresh_cookie_name)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token supplied")
    return token



@router.post("/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> RegisterOut:
    """Self-service account creation — did not exist before the free-tier
    demo (see RegisterRequest's own docstring for why role is hardcoded
    rather than accepted from the caller).

    No `mfa_secret` is set here, deliberately: with `require_mfa=True` a
    self-registered account would need `/mfa/enroll` before it could ever
    log in, which is a real, already-built path (Login.tsx links to it) —
    just not one this route forces on a demo where MFA is currently off.
    """
    existing = db.query(Clinician).filter(Clinician.email == payload.email).one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")

    clinician = Clinician(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role="doctor",
        is_active=True,
    )
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return RegisterOut(id=clinician.id)


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> TokenResponse:
    """P0-8: multi-factor authentication for clinician access — password
    AND a valid TOTP code are both required; either failing alone returns
    the same 401 to avoid leaking which factor was wrong.

    ⚠️ That requirement is currently suspended by `settings.require_mfa`
    (`app/core/config.py`), a demo-stage toggle: no phone in hand, and
    self-service accounts (`POST /auth/register`, below) have no
    enrollment path yet. `require_mfa=True` restores the check above
    immediately, no migration or re-enrollment needed — nothing about an
    account's `mfa_secret` changes while the toggle is off.

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
    except (RateLimitedError, AccountLockedError) as exc:
        # Retry-After is the standard HTTP header for exactly this (RFC
        # 9110 §10.2.3) — seconds until the client may retry, computed
        # from the actual oldest counted attempt rather than the fixed
        # window length, so it counts down accurately instead of always
        # showing the full window.
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    clinician = db.query(Clinician).filter(Clinician.email == payload.email).one_or_none()
    require_mfa = get_settings().require_mfa

    mfa_ok = (not require_mfa) or (
        clinician is not None
        and clinician.mfa_secret is not None
        and payload.mfa_code is not None
        and verify_mfa_code(clinician.mfa_secret, payload.mfa_code)
    )
    credentials_valid = (
        clinician is not None
        and clinician.is_active
        and verify_password(payload.password, clinician.hashed_password)
        and mfa_ok
    )
    record_login_attempt(db, email=payload.email, ip_address=ip_address, successful=credentials_valid)

    if not credentials_valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials or MFA code")

    # credentials_valid's AND-chain already proved clinician is not None (and
    # has an mfa_secret) — mypy can't trace that through a stored boolean, so
    # this makes the already-true invariant explicit rather than leaving it
    # for the type checker to (wrongly) flag as unverified.
    assert clinician is not None

    # Decision 0034: upgrade the stored credential in place, now that the
    # plaintext has been proven correct and exists for exactly this instant.
    # Without this, argon2 would apply only to accounts created after the
    # migration and every existing bcrypt hash would stay bcrypt forever —
    # the migration would look done while changing nothing for real users.
    # Deliberately after record_login_attempt: a rehash is a consequence of a
    # successful login, never a precondition for recording one.
    if password_needs_rehash(clinician.hashed_password):
        clinician.hashed_password = hash_password(payload.password)
        db.add(clinician)
        db.commit()
        db.refresh(clinician)

    access_token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})
    refresh_token, _ = issue_refresh_token(db, clinician.id)
    _set_refresh_cookie(response, refresh_token)
    # Still returned in the body for non-browser callers and the Phase 0.3
    # tests. The web client ignores this field and relies on the cookie —
    # see apps/web/src/lib/auth.ts, which never reads it.
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    payload: RefreshRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> TokenResponse:
    """Silent-renewal endpoint the mobile client calls when its access
    token is expired (or about to be) — no password/MFA re-entry, no
    Authorization header. Rotates the refresh token on every use; a
    replayed/stolen refresh token gets caught here, not at login.
    """
    presented = _read_refresh_token(request, payload.refresh_token)
    try:
        new_refresh_token, row = rotate_refresh_token(db, presented)
    except RefreshTokenInvalidError as exc:
        # Clear the cookie on failure: a rejected refresh token is either
        # expired, revoked, or a detected reuse, and in every case leaving
        # the dead value in the browser guarantees the next silent-renewal
        # attempt fails the same way instead of falling through to a real
        # login. Carried on the exception, not on `response` — see
        # _clear_cookie_headers.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(exc), headers=_clear_cookie_headers()
        ) from exc

    clinician = db.get(Clinician, row.clinician_id)
    if clinician is None or not clinician.is_active:
        # Deactivated mid-session: same reasoning as above, the browser
        # must not keep a credential it can never successfully use.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Clinician not found or inactive", headers=_clear_cookie_headers()
        )

    access_token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})
    _set_refresh_cookie(response, new_refresh_token)
    return TokenResponse(access_token=access_token, refresh_token=new_refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    payload: LogoutRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> None:
    """Revokes exactly the one refresh token presented — signing out
    this device without touching any other session the clinician has.
    """
    # Same precedence as _read_refresh_token, and for the sharper reason:
    # logout must revoke the session the caller named, not whichever one
    # the cookie happens to hold.
    token = payload.refresh_token or request.cookies.get(get_settings().refresh_cookie_name)
    if token:
        revoke_refresh_token(db, token)
    # Cleared unconditionally: logout must leave the browser with no
    # credential even if the token was already revoked server-side.
    _clear_refresh_cookie(response)


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
