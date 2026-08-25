"""Phase 0.3: auth surviving a real clinic day.

Covers refresh-token issuance/rotation/reuse-detection, single- and
all-session revocation (the lost-phone path), login rate limiting +
account lockout, and the two-step MFA enrollment flow.
"""

import pyotp
import pytest

from app.core.security import decode_access_token, generate_mfa_secret, hash_password
from app.models.clinician import Clinician

_PASSWORD = "correct-horse-battery-staple"


def _seed_clinician(db, *, email: str = "doc@example.com", role: str = "doctor", with_mfa: bool = True) -> Clinician:
    mfa_secret = generate_mfa_secret() if with_mfa else None
    clinician = Clinician(
        email=email,
        full_name="Dr. Reyes",
        hashed_password=hash_password(_PASSWORD),
        mfa_secret=mfa_secret,
        role=role,
    )
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician


def _login(client, clinician: Clinician) -> dict:
    code = pyotp.TOTP(clinician.mfa_secret).now()
    response = client.post(
        "/api/v1/auth/login",
        json={"email": clinician.email, "password": _PASSWORD, "mfa_code": code},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- login now issues a refresh token alongside the access token ----------


def test_login_issues_refresh_token(db, client):
    clinician = _seed_clinician(db)
    tokens = _login(client, clinician)
    assert tokens["access_token"]
    assert tokens["refresh_token"]


# --- refresh: rotation and reuse detection ---------------------------------


def test_refresh_issues_new_pair(db, client):
    clinician = _seed_clinician(db)
    tokens = _login(client, clinician)

    response = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert response.status_code == 200
    new_tokens = response.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]  # the rotating credential — must always change
    # The access token is a stateless JWT: same subject/role/expiry-second
    # inputs deterministically produce the same signed string, so an
    # unchanged access_token here isn't a bug — what must be true is that
    # it's still valid and still names this clinician.
    payload = decode_access_token(new_tokens["access_token"])
    assert payload is not None
    assert payload["sub"] == clinician.id


def test_reusing_a_rotated_refresh_token_is_rejected(db, client):
    clinician = _seed_clinician(db)
    tokens = _login(client, clinician)
    client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})  # rotates it

    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert replay.status_code == 401


def test_reusing_a_rotated_token_revokes_the_whole_session_family(db, client):
    """Two "devices" log in independently; replaying one device's
    already-rotated token must kill the other device's session too —
    that's the actual point of reuse detection.
    """
    clinician = _seed_clinician(db)
    device_a = _login(client, clinician)
    device_b = _login(client, clinician)

    client.post("/api/v1/auth/refresh", json={"refresh_token": device_a["refresh_token"]})  # A rotates once
    client.post("/api/v1/auth/refresh", json={"refresh_token": device_a["refresh_token"]})  # replay of retired token

    # device_b's still-unrotated token must now be dead too.
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": device_b["refresh_token"]})
    assert response.status_code == 401


def test_refresh_with_unknown_token_is_rejected(db, client):
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert response.status_code == 401


# --- logout: single-session revocation -------------------------------------


def test_logout_revokes_only_that_session(db, client):
    clinician = _seed_clinician(db)
    device_a = _login(client, clinician)
    device_b = _login(client, clinician)

    logout_response = client.post("/api/v1/auth/logout", json={"refresh_token": device_a["refresh_token"]})
    assert logout_response.status_code == 204

    assert client.post("/api/v1/auth/refresh", json={"refresh_token": device_a["refresh_token"]}).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": device_b["refresh_token"]}).status_code == 200


# --- admin-triggered revoke-all: the lost-phone path -----------------------


def test_admin_can_revoke_all_sessions_for_a_clinician(db, client):
    doctor = _seed_clinician(db, email="doctor@example.com", role="doctor")
    admin = _seed_clinician(db, email="admin@example.com", role="admin")
    doctor_tokens = _login(client, doctor)
    admin_access_token = _login(client, admin)["access_token"]

    response = client.post(
        f"/api/v1/auth/clinicians/{doctor.id}/revoke-sessions",
        headers={"Authorization": f"Bearer {admin_access_token}"},
    )

    assert response.status_code == 200
    assert response.json()["revoked_count"] == 1
    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": doctor_tokens["refresh_token"]})
    assert refreshed.status_code == 401


def test_doctor_cannot_revoke_sessions(db, client):
    doctor = _seed_clinician(db, email="doctor@example.com", role="doctor")
    other = _seed_clinician(db, email="other@example.com", role="doctor")
    doctor_access_token = _login(client, doctor)["access_token"]

    response = client.post(
        f"/api/v1/auth/clinicians/{other.id}/revoke-sessions",
        headers={"Authorization": f"Bearer {doctor_access_token}"},
    )

    assert response.status_code == 403


# --- rate limiting (per-IP) and account lockout (per-email) ---------------


def test_login_rate_limited_after_threshold_from_one_ip(db, client):
    # Different emails so the per-email lockout (threshold 5) doesn't
    # trip before the per-IP rate limit (threshold 10) does — isolates
    # the mechanism under test.
    for i in range(10):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": f"nobody{i}@example.com", "password": "wrong", "mfa_code": "000000"},
        )
        assert response.status_code == 401

    limited = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody-final@example.com", "password": "wrong", "mfa_code": "000000"},
    )
    assert limited.status_code == 429
    assert "address" in limited.json()["detail"]


def test_account_locked_after_repeated_failures_for_one_email(db, client):
    clinician = _seed_clinician(db)

    for _ in range(5):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": clinician.email, "password": "wrong-password", "mfa_code": "000000"},
        )
        assert response.status_code == 401

    locked = client.post(
        "/api/v1/auth/login",
        json={"email": clinician.email, "password": _PASSWORD, "mfa_code": pyotp.TOTP(clinician.mfa_secret).now()},
    )
    assert locked.status_code == 429
    assert "account" in locked.json()["detail"]


# --- MFA enrollment: provision -> confirm before activating ---------------


def test_mfa_enroll_then_confirm_activates_login(db, client):
    clinician = _seed_clinician(db, with_mfa=False)
    assert clinician.mfa_secret is None

    enroll = client.post("/api/v1/auth/mfa/enroll", json={"email": clinician.email, "password": _PASSWORD})
    assert enroll.status_code == 200
    provisioning_uri = enroll.json()["provisioning_uri"]
    secret = dict(part.split("=") for part in provisioning_uri.split("?", 1)[1].split("&"))["secret"]

    # Login must still fail — enrollment is pending, not active.
    db.refresh(clinician)
    assert clinician.mfa_secret is None

    code = pyotp.TOTP(secret).now()
    confirm = client.post(
        "/api/v1/auth/mfa/enroll/confirm",
        json={"email": clinician.email, "password": _PASSWORD, "code": code},
    )
    assert confirm.status_code == 200
    assert confirm.json()["enrolled"] is True

    db.refresh(clinician)
    assert clinician.mfa_secret == secret
    assert clinician.mfa_secret_pending is None

    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": clinician.email, "password": _PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
    )
    assert login_response.status_code == 200


def test_mfa_enroll_rejected_if_already_enrolled(db, client):
    clinician = _seed_clinician(db, with_mfa=True)

    response = client.post("/api/v1/auth/mfa/enroll", json={"email": clinician.email, "password": _PASSWORD})

    assert response.status_code == 409


def test_mfa_enroll_confirm_rejects_wrong_code(db, client):
    clinician = _seed_clinician(db, with_mfa=False)
    client.post("/api/v1/auth/mfa/enroll", json={"email": clinician.email, "password": _PASSWORD})

    response = client.post(
        "/api/v1/auth/mfa/enroll/confirm",
        json={"email": clinician.email, "password": _PASSWORD, "code": "000000"},
    )

    assert response.status_code == 401
    db.refresh(clinician)
    assert clinician.mfa_secret is None  # still not activated
