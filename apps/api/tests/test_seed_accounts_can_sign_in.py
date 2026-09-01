"""The staging seed's accounts must be able to reach the app.

This file exists because they could not, for two independent reasons, and
neither was caught by 419 passing tests:

1. `seed_staging.py` created clinicians with **no `mfa_secret`**, and login
   requires one (Phase 0.3).
2. Every account was on `@staging.remedy.invalid`, and `email-validator` —
   which Pydantic's `EmailStr` uses, and which `LoginRequest.email` is typed
   as — rejects `.invalid` as a special-use TLD. So the request was refused
   at the schema with a 422 before any credential was checked.

Nothing noticed because **every existing API test mints its own token with
`create_access_token` and never goes through the login screen.** That is a
reasonable thing for a route test to do, and it left the one path a human
actually uses completely unexercised. Meanwhile
`docs/runbooks/staging.md` instructed the reader to sign in as the doctor —
a documented step that could not succeed.

So these tests deliberately use the real endpoint and the seed's real
constants, rather than a fixture that would drift from them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pyotp
import pytest
from pydantic import BaseModel, EmailStr, ValidationError

from app.core.security import hash_password
from app.models.clinician import Clinician

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import seed_staging  # noqa: E402


def _seed_style_clinician(db, role: str, prefix: str) -> Clinician:
    """A clinician built exactly the way `seed_staging.seed()` builds one.

    Imports the constants rather than repeating their values, so a change to
    the seed's domain, password or MFA secret is exercised here instead of
    silently diverging.
    """
    clinician = Clinician(
        email=f"{prefix}@{seed_staging.SYNTHETIC_EMAIL_DOMAIN}",
        full_name="Seeded Account",
        hashed_password=hash_password(seed_staging.SYNTHETIC_PASSWORD),
        mfa_secret=seed_staging.SYNTHETIC_MFA_SECRET,
        role=role,
    )
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician


@pytest.mark.parametrize(
    ("prefix", "role"),
    [("doctor", "doctor"), ("compliance", "compliance"), ("admin", "admin")],
)
def test_a_seeded_account_can_actually_log_in(db, client, prefix, role):
    """The whole point. Through `POST /auth/login`, not a minted token."""
    _seed_style_clinician(db, role, prefix)

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": f"{prefix}@{seed_staging.SYNTHETIC_EMAIL_DOMAIN}",
            "password": seed_staging.SYNTHETIC_PASSWORD,
            "mfa_code": pyotp.TOTP(seed_staging.SYNTHETIC_MFA_SECRET).now(),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


def test_the_seed_domain_passes_email_validation():
    """Guards bug 2 directly, at the layer that broke.

    `.invalid`, `.test` and `.localhost` are all RFC 2606 reserved *and* all
    rejected by email-validator as special-use. `.example` is reserved
    without being special-use, which is the only reason it works — so this
    asserts the property rather than the string, and would fail if someone
    "hardened" the domain back to `.invalid`.
    """

    class _LoginEmail(BaseModel):
        email: EmailStr

    _LoginEmail(email=f"doctor@{seed_staging.SYNTHETIC_EMAIL_DOMAIN}")

    for special_use in ("invalid", "test", "localhost"):
        with pytest.raises(ValidationError):
            _LoginEmail(email=f"doctor@staging.remedy.{special_use}")


def test_the_seed_domain_is_still_unroutable_so_lock_4_holds():
    """The safety property must survive the fix.

    Lock 4 refuses to seed a database containing any clinician *not* on this
    domain, on the grounds that no real clinic would ever use it. That
    argument depends on the domain being RFC 2606 reserved, which `.example`
    is — swapping to a routable domain like `staging.remedy.com` would pass
    the test above while quietly destroying the lock.
    """
    assert seed_staging.SYNTHETIC_EMAIL_DOMAIN.endswith(".example")


def test_the_seed_mfa_secret_is_usable_by_an_authenticator_app():
    """A developer types this into a TOTP app, so it has to be valid base32
    that `pyotp` accepts — an invalid secret would fail at login with the
    same opaque 401 as a wrong code.
    """
    totp = pyotp.TOTP(seed_staging.SYNTHETIC_MFA_SECRET)
    assert totp.verify(totp.now())


def test_a_wrong_mfa_code_is_still_refused(db, client):
    """The fix must not have opened the door: a real secret with a wrong code
    is still a failed login, not a pass.
    """
    _seed_style_clinician(db, "doctor", "doctor")

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": f"doctor@{seed_staging.SYNTHETIC_EMAIL_DOMAIN}",
            "password": seed_staging.SYNTHETIC_PASSWORD,
            "mfa_code": "000000",
        },
    )

    assert response.status_code == 401


def test_the_wrong_password_is_still_refused(db, client):
    _seed_style_clinician(db, "doctor", "doctor")

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": f"doctor@{seed_staging.SYNTHETIC_EMAIL_DOMAIN}",
            "password": "not-the-seeded-password",
            "mfa_code": pyotp.TOTP(seed_staging.SYNTHETIC_MFA_SECRET).now(),
        },
    )

    assert response.status_code == 401
