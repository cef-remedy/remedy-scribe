"""Phase 4.1: the transit-security half, as far as the app itself owns it.

"TLS everywhere, HSTS, modern cipher suites" splits cleanly in two. The
certificate, the TLS floor and the cipher list belong to whatever terminates
TLS in front of FastAPI, which is Phase 5's — nothing here can assert them.
What the app owns is the set of response headers it emits, and those are
asserted here, including the two that are easy to get subtly wrong:

  * HSTS must NOT go out over plain-http localhost. A browser that receives
    it pins localhost to https for the full max-age, across every port, and
    breaks every other local project on the machine.
  * The strict API CSP must not be applied to Swagger UI, which is the one
    HTML page here and would render blank under it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import Settings
from app.main import _STATIC_SECURITY_HEADERS

_API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def response(client):
    """Any response will do — these headers are not route-specific, which is
    the point of setting them in middleware rather than per endpoint.
    """
    return client.get("/health")


def test_the_static_hardening_headers_are_on_every_response(response):
    assert response.status_code == 200
    for name, value in _STATIC_SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_the_api_declares_it_loads_nothing_and_is_framed_nowhere(response):
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"


def test_urls_carrying_patient_and_encounter_ids_are_never_sent_as_a_referer(response):
    assert response.headers["referrer-policy"] == "no-referrer"


def test_headers_are_present_on_an_error_response_too(client):
    """Middleware, not a per-route decorator: the 404 and 500 paths are
    exactly where per-endpoint hardening gets forgotten.
    """
    missing = client.get("/no-such-route")
    assert missing.status_code == 404
    assert missing.headers["x-content-type-options"] == "nosniff"


def test_phi_responses_are_marked_no_store(client):
    """Anything under the API prefix can carry PHI, including the 401 that
    comes back from an unauthenticated read. Nothing there belongs in a
    browser or proxy cache.
    """
    phi_route = client.get("/api/v1/patients/search?query=cruz")
    assert phi_route.headers["cache-control"] == "no-store"


def test_hsts_is_not_announced_over_plain_http_in_development(response):
    """Not a missing feature — sending HSTS from http://localhost pins
    localhost to https for two years for every project on the machine, and
    there is no undo short of browser internals.
    """
    assert "strict-transport-security" not in response.headers


def test_hsts_is_announced_when_the_proxy_says_the_browser_leg_was_tls(client):
    """TLS terminates in front of the app (Phase 5), so X-Forwarded-Proto is
    the only evidence the app ever has that the request arrived encrypted.
    """
    tls = client.get("/health", headers={"X-Forwarded-Proto": "https"})
    hsts = tls.headers["strict-transport-security"]
    assert "max-age=63072000" in hsts
    assert "includeSubDomains" in hsts
    # Preload is opt-in: submitting a domain to the browser preload list is
    # close to irreversible and is Remedy's call, not a default.
    assert "preload" not in hsts


def test_the_hsts_directives_follow_settings():
    settings = Settings(hsts_max_age_seconds=300, hsts_include_subdomains=False, hsts_preload=False)
    from app.main import SecurityHeadersMiddleware

    middleware = SecurityHeadersMiddleware(app=None, settings=settings)  # type: ignore[arg-type]
    assert middleware._hsts_value == "max-age=300"


def test_swagger_ui_is_exempt_from_the_api_csp(client):
    """Served only outside production, and the strict CSP would blank it."""
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "content-security-policy" not in docs.headers


def test_cors_still_works_alongside_the_hardening(client):
    """The header middleware sits outside CORSMiddleware, so a preflight is
    answered by CORS and still leaves with the hardening headers attached.
    """
    preflight = client.options(
        "/api/v1/patients/search",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert preflight.headers["x-content-type-options"] == "nosniff"


# --- Production posture, asserted in a real process ----------------------


def _boot(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    """Import the app in a clean subprocess. cwd is a temp directory because
    Settings reads a relative `.env`, and a developer's own .env must not
    decide the outcome of these assertions.
    """
    return subprocess.run(
        [sys.executable, "-c", "import app.main; print('DOCS', app.main.app.docs_url)"],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "PYTHONPATH": str(_API_ROOT),
            "S3_PROVISION_BUCKET_ON_STARTUP": "false",
            **env,
        },
        capture_output=True,
        text=True,
    )


_VALID_PRODUCTION = {
    "ENVIRONMENT": "production",
    "PHI_ENCRYPTION_KEY": "Yqf1oOvA0mVv0bWb0m5xUYo9J2ZG4x8rN0m7bqjHhVo=",
    "JWT_SECRET": "a-real-production-jwt-secret-value",
    "S3_SECRET_KEY": "a-real-production-object-store-secret",
    "REFRESH_COOKIE_SECURE": "true",
    "CORS_ALLOW_ORIGINS": "https://scribe.remedy.example",
}


def test_production_refuses_a_refresh_cookie_that_is_not_secure_flagged(tmp_path):
    """Production is served over TLS, so a session credential without the
    Secure flag is one the browser would happily send in the clear.
    """
    result = _boot(tmp_path, **{**_VALID_PRODUCTION, "REFRESH_COOKIE_SECURE": "false"})
    assert result.returncode != 0
    assert "REFRESH_COOKIE_SECURE" in result.stderr


def test_production_refuses_a_cors_allow_list_still_naming_localhost(tmp_path):
    """A production allow-list pointing at a developer's Vite server means a
    development .env was deployed — and whatever else came with it.
    """
    result = _boot(
        tmp_path,
        **{**_VALID_PRODUCTION, "CORS_ALLOW_ORIGINS": "https://scribe.remedy.example,http://localhost:5173"},
    )
    assert result.returncode != 0
    assert "CORS_ALLOW_ORIGINS" in result.stderr


def test_production_serves_no_interactive_docs(tmp_path):
    """Swagger UI publishes a complete map of the PHI endpoints, and is the
    only page needing the CSP relaxed. Neither is worth keeping in the
    environment that holds patient data.
    """
    result = _boot(tmp_path, **_VALID_PRODUCTION)
    assert result.returncode == 0, result.stderr
    assert "DOCS None" in result.stdout
