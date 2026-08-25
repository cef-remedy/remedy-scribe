from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.security import create_access_token, verify_mfa_code, verify_password
from app.models.clinician import Clinician
from app.schemas.clinician import LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """P0-8: multi-factor authentication for clinician access — password
    AND a valid TOTP code are both required; either failing alone returns
    the same 401 to avoid leaking which factor was wrong.
    """
    clinician = db.query(Clinician).filter(Clinician.email == payload.email).one_or_none()

    if (
        clinician is None
        or not clinician.is_active
        or not verify_password(payload.password, clinician.hashed_password)
        or not clinician.mfa_secret
        or not verify_mfa_code(clinician.mfa_secret, payload.mfa_code)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials or MFA code")

    token = create_access_token(subject=clinician.id, extra_claims={"role": clinician.role})
    return TokenResponse(access_token=token)
