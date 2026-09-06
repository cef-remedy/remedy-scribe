"""Self-service registration and the `require_mfa` demo toggle.

Both are new: there was no way to create a clinician account except the
seed script, and MFA was unconditionally required at login. Found live,
deploying a free-tier demo with no phone in hand and no time to build a
real MFA rollout for self-registered accounts yet.
"""
import pyotp

from app.core.config import get_settings
from app.core.security import generate_mfa_secret, hash_password
from app.models.clinician import Clinician


def _seed_clinician(db, *, password: str = "correct-horse-battery-staple", with_mfa: bool = True):
    clinician = Clinician(
        email="doc@example.com",
        full_name="Dr. Reyes",
        hashed_password=hash_password(password),
        mfa_secret=generate_mfa_secret() if with_mfa else None,
        role="doctor",
    )
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician


# --- registration ----------------------------------------------------------


def test_register_creates_a_doctor_account_with_no_mfa_secret(db, client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "new-doc@example.com", "password": "a-real-password", "full_name": "Dr. Cruz"},
    )
    assert response.status_code == 201

    clinician = db.query(Clinician).filter(Clinician.email == "new-doc@example.com").one()
    assert clinician.role == "doctor"
    assert clinician.mfa_secret is None


def test_register_refuses_a_role_field_entirely(db, client):
    """RegisterRequest has no role field at all -- confirming the schema
    itself, not just that the route ignores an attempt, since an ignored
    field and a rejected one look identical from a single successful
    response."""
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "hopeful-admin@example.com",
            "password": "a-real-password",
            "full_name": "Someone",
            "role": "admin",
        },
    )
    assert response.status_code == 201
    assert db.query(Clinician).filter(Clinician.email == "hopeful-admin@example.com").one().role == "doctor"


def test_register_refuses_a_duplicate_email(db, client):
    _seed_clinician(db)
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "doc@example.com", "password": "a-real-password", "full_name": "Someone Else"},
    )
    assert response.status_code == 409


def test_register_refuses_a_short_password(client):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "short-pw@example.com", "password": "short", "full_name": "Someone"},
    )
    assert response.status_code == 422


# --- the require_mfa toggle --------------------------------------------------


def test_with_mfa_required_a_missing_code_is_refused(db, client):
    _seed_clinician(db)
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "doc@example.com", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 401


def test_with_require_mfa_false_password_alone_logs_in_even_with_a_secret_set(db, client, monkeypatch):
    """The account already has an mfa_secret (as every seeded account
    does) -- the toggle must skip the check globally, not just for
    accounts that happen to have none."""
    monkeypatch.setattr(get_settings(), "require_mfa", False, raising=False)
    _seed_clinician(db, with_mfa=True)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "doc@example.com", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 200
    assert "access_token" in response.json()


def test_with_require_mfa_false_a_self_registered_account_can_log_in(db, client, monkeypatch):
    monkeypatch.setattr(get_settings(), "require_mfa", False, raising=False)
    client.post(
        "/api/v1/auth/register",
        json={"email": "self-reg@example.com", "password": "a-real-password", "full_name": "Dr. Santos"},
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "self-reg@example.com", "password": "a-real-password"},
    )
    assert response.status_code == 200


def test_wrong_password_still_fails_regardless_of_the_mfa_toggle(db, client, monkeypatch):
    """The toggle removes one factor; it must not become a second way
    past the one that's left."""
    monkeypatch.setattr(get_settings(), "require_mfa", False, raising=False)
    _seed_clinician(db)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "doc@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_require_mfa_true_is_unaffected_by_a_present_but_wrong_code(db, client):
    """Baseline, unchanged behaviour: confirms the default (require_mfa's
    own field default) still rejects a wrong code, so the toggle's
    presence hasn't loosened the normal path."""
    _seed_clinician(db)
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "doc@example.com", "password": "correct-horse-battery-staple", "mfa_code": "000000"},
    )
    assert response.status_code == 401


def test_require_mfa_true_still_accepts_a_correct_code(db, client):
    monkeypatch_secret = generate_mfa_secret()
    clinician = Clinician(
        email="doc2@example.com",
        full_name="Dr. Reyes",
        hashed_password=hash_password("correct-horse-battery-staple"),
        mfa_secret=monkeypatch_secret,
        role="doctor",
    )
    db.add(clinician)
    db.commit()

    code = pyotp.TOTP(monkeypatch_secret).now()
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "doc2@example.com", "password": "correct-horse-battery-staple", "mfa_code": code},
    )
    assert response.status_code == 200
