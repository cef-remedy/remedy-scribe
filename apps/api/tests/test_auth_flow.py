import pyotp

from app.core.security import generate_mfa_secret, hash_password
from app.models.clinician import Clinician


def _seed_clinician(db, *, password: str = "correct-horse-battery-staple") -> tuple[Clinician, str]:
    mfa_secret = generate_mfa_secret()
    clinician = Clinician(
        email="doc@example.com",
        full_name="Dr. Reyes",
        hashed_password=hash_password(password),
        mfa_secret=mfa_secret,
        role="doctor",
    )
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician, mfa_secret


def test_login_requires_correct_password_and_mfa(db, client):
    _seed_clinician(db)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "doc@example.com", "password": "wrong-password", "mfa_code": "000000"},
    )

    assert response.status_code == 401


def test_login_succeeds_and_token_unlocks_protected_route(db, client):
    clinician, mfa_secret = _seed_clinician(db, password="another-pass")

    code = pyotp.TOTP(mfa_secret).now()
    response = client.post(
        "/api/v1/auth/login",
        json={"email": clinician.email, "password": "another-pass", "mfa_code": code},
    )
    assert response.status_code == 200
    token = response.json()["access_token"]

    protected = client.get("/api/v1/encounters/loose", headers={"Authorization": f"Bearer {token}"})
    assert protected.status_code == 200
    assert protected.json() == []
