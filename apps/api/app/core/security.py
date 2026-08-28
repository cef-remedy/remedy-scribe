"""Auth primitives and PHI field-level encryption.

Covers P0-8 (security baseline): password hashing, JWT issuance, and
TOTP-based MFA. Also defines EncryptedString, a SQLAlchemy TypeDecorator
used on PHI columns (patient name, birthdate, transcript text) for
column-level encryption-at-rest — separate from and in addition to
whatever disk/volume encryption the deployment target provides.

docs/tech-stack.md specifies pgcrypto for this; EncryptedString is an
application-layer equivalent that works identically against Postgres and
SQLite (so the test suite doesn't require a live Postgres), encrypting
before the value ever reaches the driver. Phase 4.1 confirmed that
divergence as a decision rather than leaving it an accident — see
docs/decisions/0031-phi-encryption-stays-in-the-application-layer.md, which
also records that the final choice between this, pgcrypto and KMS envelope
encryption belongs to Remedy's DPO, not to this implementation.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import argon2
import bcrypt
import pyotp
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, MultiFernet
from jose import JWTError, jwt
from sqlalchemy import String, Text
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings


def build_phi_cipher(primary_key: str, previous_keys: list[str] | None = None) -> MultiFernet:
    """The PHI cipher, over an explicitly supplied key set.

    MultiFernet is what makes rotation possible without downtime: it
    *encrypts* with the first key only, and on *decrypt* tries each key in
    turn (verified against cryptography 43.0.1's own source — MultiFernet.
    encrypt delegates to `self._fernets[0]`, decrypt loops and re-raises
    InvalidToken only when every key fails). So during a rotation, with the
    new key first and the outgoing key second, the app writes new
    ciphertext while still reading everything the rewrite has not reached.

    Kept separate from phi_cipher() below so scripts/rotate_phi_key.py can
    hold two differently-ordered cipher sets in one process without
    touching, or being confused by, the app's cached settings singleton.
    """
    return MultiFernet([Fernet(k.encode()) for k in [primary_key, *(previous_keys or [])]])


@lru_cache(maxsize=1)
def phi_cipher() -> MultiFernet:
    """The process-wide cipher, from settings.

    Cached because it is constructed on *every* encrypted column read and
    write — decision 0029 measured a single patient search decrypting the
    whole directory, and rebuilding the key schedule per value there is
    pure overhead. Settings are an lru_cache singleton anyway, so the
    inputs cannot change under a running process; a test that swaps keys
    must call reset_phi_cipher_cache().
    """
    settings = get_settings()
    key = settings.phi_encryption_key
    if not key:
        raise RuntimeError("PHI_ENCRYPTION_KEY is not set — refusing to read/write an encrypted PHI column without it.")
    return build_phi_cipher(key, settings.phi_previous_key_list)


def reset_phi_cipher_cache() -> None:
    """For tests and for the rotation script's post-rotation verification,
    which both need the cipher rebuilt after the key set changes.
    """
    phi_cipher.cache_clear()


# P0-8, decision 0034. Argon2id for new credentials; bcrypt kept
# verify-only so anything hashed before this change still logs in.
# passlib is gone: 1.7.4 (Oct 2020) is unmaintained, and its bcrypt
# backend is the sole reason requirements.txt pinned `bcrypt==4.0.1` --
# a security-critical library held backwards to keep a dead wrapper's
# start-up self-test passing. Calling both hashers directly removes the
# reason for the pin instead of carrying it forward.
_argon2 = argon2.PasswordHasher()

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")
# bcrypt ignores everything past 72 bytes, and legacy hashes were
# produced under that truncation -- so verification has to reproduce it.
# bcrypt>=4.1 raises rather than truncating silently, which would turn a
# correct long password into a 500 instead of a login.
_BCRYPT_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    return _argon2.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if hashed.startswith("$argon2"):
        try:
            return _argon2.verify(hashed, plain)
        except (VerificationError, InvalidHashError):
            return False
    if hashed.startswith(_BCRYPT_PREFIXES):
        try:
            return bcrypt.checkpw(plain.encode()[:_BCRYPT_MAX_BYTES], hashed.encode())
        except ValueError:
            return False
    # Anything else is a corrupt or placeholder row, not a match. False
    # rather than an exception: a malformed stored hash should be a failed
    # login, not a 500 that tells an attacker this account is different.
    return False


def password_needs_rehash(hashed: str) -> bool:
    """True when an already-verified password should be stored again: a
    bcrypt credential predating decision 0034, or an argon2 hash below the
    current cost parameters.

    Deliberately not folded into verify_password. The plaintext exists
    only for the instant of a successful login, and only the caller
    (app/api/routes/auth.py) holds the session to write the upgrade back.
    """
    if hashed.startswith("$argon2"):
        try:
            return _argon2.check_needs_rehash(hashed)
        except InvalidHashError:
            return True
    return True


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


def mfa_provisioning_uri(secret: str, *, account_email: str) -> str:
    """The otpauth:// URI an authenticator app scans as a QR code, for
    the enrollment flow (Phase 0.3) — replaces "the TOTP secret can only
    be created by a seed script."
    """
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account_email, issuer_name="Remedy Scribe")


def verify_mfa_code(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def generate_refresh_token() -> str:
    """A high-entropy opaque value — not a JWT. Deliberately unstructured
    so it can never be mistaken for (or misused as) an access token; its
    only job is to be looked up by hash against RefreshToken.token_hash.
    """
    return secrets.token_urlsafe(32)


def hash_refresh_token(raw_token: str) -> str:
    """SHA-256, not bcrypt: this is a ~256-bit random value, not a
    low-entropy human password, so there's no offline-guessing risk to
    slow down — a fast, deterministic hash is what makes an indexed
    lookup by hash possible at all.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


class EncryptedString(TypeDecorator):
    """A String column that is Fernet-encrypted at rest.

    Requires settings.phi_encryption_key (32-byte urlsafe-base64, generate
    with `Fernet.generate_key()`). Raises at first use if unset, rather than
    silently storing PHI in plaintext.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return phi_cipher().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return phi_cipher().decrypt(value.encode()).decode()


class EncryptedJSON(TypeDecorator):
    """The same guarantee as EncryptedString, generalized to structured
    data: JSON-serialize, then Fernet-encrypt: Text, not String, since a
    20-40 minute consult's word-level transcript can run to tens of KB.

    Used for Transcript.segments (Phase 1.2) — the transcript is PHI,
    arguably more sensitive than the note itself (verbatim, including
    what the doctor chose not to write down), so it gets the identical
    encryption-at-rest treatment as everything else PHI in this schema.

    A note on "JSONB" in the Phase 1.2 decision: that was about *shape*
    (one row holding the whole structure, vs. a row-per-word table) —
    not literally Postgres's native JSONB type. Once the payload is
    encrypted, the column is opaque ciphertext regardless of declared
    type, so native JSON path queries are off the table either way; this
    stores as plain Text, identically on Postgres and SQLite, same as
    EncryptedString already does for the same reason.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return phi_cipher().encrypt(json.dumps(value).encode()).decode()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(phi_cipher().decrypt(value.encode()).decode())
