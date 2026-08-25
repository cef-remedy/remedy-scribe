"""Auth primitives and PHI field-level encryption.

Covers P0-8 (security baseline): password hashing, JWT issuance, and
TOTP-based MFA. Also defines EncryptedString, a SQLAlchemy TypeDecorator
used on PHI columns (patient name, birthdate, transcript text) for
column-level encryption-at-rest — separate from and in addition to
whatever disk/volume encryption the deployment target provides.

docs/tech-stack.md specifies pgcrypto for this; EncryptedString is an
application-layer equivalent that works identically against Postgres and
SQLite (so the test suite doesn't require a live Postgres), encrypting
before the value ever reaches the driver. Swap to native pgcrypto
functions later if key-rotation/HSM requirements demand it (see
docs/tech-stack.md §9, deferred items) — the column contract (opaque
ciphertext at rest) stays the same either way.
"""

from datetime import datetime, timedelta, timezone

import pyotp
from cryptography.fernet import Fernet
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


def create_access_token(subject: str, extra_claims: dict | None = None) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": subject, "exp": expire, **(extra_claims or {})}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None


def generate_mfa_secret() -> str:
    """One TOTP secret per clinician, provisioned once and stored encrypted."""
    return pyotp.random_base32()


def verify_mfa_code(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)


class EncryptedString(TypeDecorator):
    """A String column that is Fernet-encrypted at rest.

    Requires settings.phi_encryption_key (32-byte urlsafe-base64, generate
    with `Fernet.generate_key()`). Raises at first use if unset, rather than
    silently storing PHI in plaintext.
    """

    impl = String
    cache_ok = True

    def _fernet(self) -> Fernet:
        key = get_settings().phi_encryption_key
        if not key:
            raise RuntimeError(
                "PHI_ENCRYPTION_KEY is not set — refusing to read/write an "
                "encrypted PHI column without it."
            )
        return Fernet(key.encode())

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return self._fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return self._fernet().decrypt(value.encode()).decode()
