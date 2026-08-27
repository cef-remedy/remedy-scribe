"""Phase 2.1: the backend changes a browser client needs that a native one
never did (decision 0024).

Two things, both of which fail *silently* if wrong — which is why they get
tests rather than a manual check:

- **CORS.** Without it the preflight is rejected and the request never
  reaches a route, so nothing appears in the API log at all. The failure
  looks like a frontend bug.
- **The httpOnly refresh cookie.** If `httponly` were dropped the app
  would keep working perfectly while silently becoming XSS-readable —
  the exact property that made moving off `expo-secure-store` worthwhile.
"""

import pyotp

from app.core.config import get_settings
from app.core.security import generate_mfa_secret, hash_password
from app.models.clinician import Clinician

_PASSWORD = "correct-horse-battery"


def _seed_doctor(db, email: str = "doc@example.com") -> tuple[Clinician, str]:
    secret = generate_mfa_secret()
    clinician = Clinician(
        email=email,
        full_name="Dr. Reyes",
        hashed_password=hash_password(_PASSWORD),
        role="doctor",
        mfa_secret=secret,
    )
    db.add(clinician)
    db.commit()
    db.refresh(clinician)
    return clinician, secret


def _login(client, email: str, secret: str):
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": _PASSWORD, "mfa_code": pyotp.TOTP(secret).now()},
    )


# --- CORS -----------------------------------------------------------------


def test_preflight_from_an_allowed_origin_is_permitted(client):
    origin = get_settings().cors_origin_list[0]
    response = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    # Required for the refresh cookie to be sent at all.
    assert response.headers["access-control-allow-credentials"] == "true"


def test_preflight_from_an_unknown_origin_is_not_granted(client):
    response = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )

    # Starlette answers the preflight, but must not hand the caller an
    # allow-origin header for an origin outside the allow-list.
    assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_allow_origin_is_never_a_wildcard(client):
    """A wildcard is mutually exclusive with credentialed requests per the
    CORS spec, so if this ever became "*" the refresh cookie would stop
    being sent and silent renewal would break in the browser only.
    """
    origin = get_settings().cors_origin_list[0]
    response = client.get("/health", headers={"Origin": origin})
    assert response.headers.get("access-control-allow-origin") != "*"


# --- the refresh cookie ---------------------------------------------------


def test_login_sets_an_httponly_refresh_cookie(client, db):
    _clinician, secret = _seed_doctor(db)
    response = _login(client, "doc@example.com", secret)

    assert response.status_code == 200
    name = get_settings().refresh_cookie_name
    assert name in response.cookies

    raw = response.headers["set-cookie"]
    # The whole point of the move off expo-secure-store: unreadable by JS.
    assert "httponly" in raw.lower()
    # Scoped to the auth routes; no other endpoint should receive it.
    assert "path=/api/v1/auth" in raw.lower()


def test_access_token_is_returned_in_the_body_not_a_cookie(client, db):
    """Decisions 0006/0007 are unchanged by the move to a browser: the
    access token stays short-lived and in memory. Putting it in a cookie
    would make it ambient on every request and undo that.
    """
    _clinician, secret = _seed_doctor(db)
    response = _login(client, "doc@example.com", secret)

    body = response.json()
    assert body["access_token"]
    assert get_settings().refresh_cookie_name not in body.get("access_token", "")
    raw = response.headers["set-cookie"].lower()
    assert "access_token" not in raw


def test_refresh_works_from_the_cookie_alone(client, db):
    """The browser client never reads the refresh token, so it sends an
    empty body — this is the path that actually runs in production.
    """
    _clinician, secret = _seed_doctor(db)
    assert _login(client, "doc@example.com", secret).status_code == 200

    response = client.post("/api/v1/auth/refresh", json={})

    assert response.status_code == 200
    assert response.json()["access_token"]
    assert get_settings().refresh_cookie_name in response.cookies  # rotated


def test_refresh_with_no_token_at_all_is_401(client, db):
    _seed_doctor(db)
    client.cookies.clear()
    response = client.post("/api/v1/auth/refresh", json={})
    assert response.status_code == 401


def test_an_explicit_body_token_takes_precedence_over_the_cookie(client, db):
    """Body-first precedence, asserted directly rather than left implicit.

    Getting this backwards silently broke Phase 0.3's reuse detection: a
    caller naming a deliberately-stale token got the valid cookie rotated
    instead, returning 200 where 401 was required.
    """
    _clinician, secret = _seed_doctor(db)
    first = _login(client, "doc@example.com", secret)
    stale = first.json()["refresh_token"]

    # Rotate once via the cookie so `stale` is now a used token, while the
    # cookie holds a fresh one.
    assert client.post("/api/v1/auth/refresh", json={}).status_code == 200

    # Presenting the stale token explicitly must be judged on its own
    # merits, not quietly upgraded to the cookie's fresher token.
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": stale})
    assert replay.status_code == 401


def test_logout_clears_the_cookie(client, db):
    _clinician, secret = _seed_doctor(db)
    assert _login(client, "doc@example.com", secret).status_code == 200

    response = client.post("/api/v1/auth/logout", json={})

    assert response.status_code == 204
    # Cleared by expiry, so the browser drops it.
    raw = response.headers.get("set-cookie", "").lower()
    assert get_settings().refresh_cookie_name in raw
    assert "expires=thu, 01 jan 1970" in raw or "max-age=0" in raw


def test_a_rejected_refresh_clears_the_cookie(client, db):
    """A dead cookie left in place guarantees every future silent renewal
    fails the same way instead of falling through to a real login.
    """
    _clinician, secret = _seed_doctor(db)
    assert _login(client, "doc@example.com", secret).status_code == 200

    # Force a reuse-detection failure using the cookie path.
    stale = _login(client, "doc@example.com", secret).json()["refresh_token"]
    client.post("/api/v1/auth/refresh", json={})  # rotates the cookie
    client.cookies.set(get_settings().refresh_cookie_name, stale)

    response = client.post("/api/v1/auth/refresh", json={})

    assert response.status_code == 401
    raw = response.headers.get("set-cookie", "").lower()
    assert get_settings().refresh_cookie_name in raw
