from collections.abc import Generator

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.session import get_db as _get_db
from app.models.clinician import Clinician

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_db() -> Generator[Session, None, None]:
    yield from _get_db()


def get_current_clinician(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Clinician:
    payload = decode_access_token(token)
    if payload is None or "sub" not in payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    clinician = db.get(Clinician, payload["sub"])
    if clinician is None or not clinician.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Clinician not found or inactive")
    return clinician


def require_role(*roles: str):
    """P0-8: role-based access control, need-to-know. Usage:
    `clinician: Clinician = Depends(require_role("compliance", "admin"))`
    """

    def _check(clinician: Clinician = Depends(get_current_clinician)) -> Clinician:
        if clinician.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role for this action")
        return clinician

    return _check
