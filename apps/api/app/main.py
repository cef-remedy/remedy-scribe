import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.routes import audit_logs, auth, consent, encounters, health, notes, patients, pilot, uploads
from app.core.config import Settings, get_settings
from app.core.observability import (
    CORRELATION_HEADER,
    CorrelationIdMiddleware,
    configure_logging,
    init_error_tracking,
    log_event,
)

settings = get_settings()

# Phase 5.2 (P0-8). Before anything else, and at import time rather than in
# `lifespan`: this installs the PHI-scrubbing log-record factory
# (app/core/observability.py), and any line logged before it runs is a line
# that never passed the scrubber. Route imports above can log at import
# time, and `Settings` validation failures are logged by uvicorn — so
# "first statement after settings" is the earliest honest place for it.
configure_logging(settings)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

# Phase 4.1 (P0-8). Everything in this block is response headers the API can
# set for itself. The other half of "TLS everywhere, HSTS, modern cipher
# suites" — the certificate, the TLS version floor, the cipher list, the
# http→https redirect — is terminated in front of FastAPI and is Phase 5's
# to configure; see docs/runbooks/key-rotation.md §"What Phase 5 still owes".

# Static headers, on every response. Values chosen for a JSON API that
# serves no HTML and is never framed, which is why they can all be at their
# strictest setting rather than negotiated against some page's needs.
_STATIC_SECURITY_HEADERS: dict[str, str] = {
    # Ciphertext and PHI JSON must never be sniffed into an executable type.
    "x-content-type-options": "nosniff",
    # Belt and braces with the CSP below: X-Frame-Options is still what old
    # browsers honour, frame-ancestors is what current ones honour.
    "x-frame-options": "DENY",
    # A URL here can carry a patient or encounter id. Sending it to any
    # third party as a Referer is a PHI disclosure through a header.
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    # An API that returns only JSON needs to load nothing and be embedded
    # nowhere, so 'none' is not a hardening compromise — it is accurate.
    "content-security-policy": ("default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
}

# The interactive docs are the one HTML surface here, and Swagger UI loads
# its own CSS/JS — the API CSP above would blank the page. They are served
# only outside production (see the FastAPI constructor below), so exempting
# them costs nothing in the environment that holds PHI.
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


class SecurityHeadersMiddleware:
    """Adds the security headers above, plus HSTS and no-store, to every
    response.

    A raw ASGI middleware rather than BaseHTTPMiddleware: this runs on
    every single request including the streamed ones, and BaseHTTPMiddleware
    wraps each response in an extra anyio task pair to do it. Rewriting
    headers on the `http.response.start` message costs nothing measurable.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        directives = [f"max-age={settings.hsts_max_age_seconds}"]
        if settings.hsts_include_subdomains:
            directives.append("includeSubDomains")
        if settings.hsts_preload:
            directives.append("preload")
        self._hsts_value = "; ".join(directives)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        # TLS terminates at the proxy, so scope["scheme"] is "http" for every
        # production request. X-Forwarded-Proto is the only evidence the app
        # has that the browser's leg was encrypted — and the header is
        # trustworthy exactly to the extent that the proxy sets it and
        # nothing else can reach the app. Phase 5 has to guarantee that; it
        # is called out explicitly in the runbook.
        forwarded_proto = _header(scope, b"x-forwarded-proto")
        over_tls = scope.get("scheme") == "https" or forwarded_proto == "https"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if path not in _DOCS_PATHS:
                    for name, value in _STATIC_SECURITY_HEADERS.items():
                        headers[name] = value
                # Never announce HSTS over a plain-http request in dev. A
                # browser that receives it pins *localhost* to https for
                # two years — across every port, breaking every other local
                # project on the machine, with no way to undo it except
                # digging through browser internals.
                if over_tls or self.settings.is_production:
                    headers["strict-transport-security"] = self._hsts_value
                # Responses under the API prefix carry PHI. setdefault, not
                # assignment, so a route that has already made a stronger or
                # more specific statement about caching keeps it.
                if path.startswith(API_PREFIX):
                    headers.setdefault("cache-control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1").strip().lower()
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Phase 1.1: idempotent bucket setup (create-if-missing, default
    encryption, retention + orphan-upload-abort lifecycle rules) at
    startup. Never fails startup — see storage.ensure_bucket_configured's
    own docstring for why a locked-down production IAM role failing this
    is expected, not fatal. Off in tests (S3_PROVISION_BUCKET_ON_STARTUP) —
    see tests/conftest.py for why.

    Phase 5.2 adds error-tracking init here rather than at import time:
    `sentry_sdk.init` opens a background transport thread, and doing that
    at module import means every `from app.main import app` in the test
    suite starts one.
    """
    # Never fatal, in either direction. A missing DSN is the normal state in
    # development and in tests; a *production* deploy without one is a real
    # gap but not a reason to refuse to serve a clinic, so it is reported
    # loudly instead of raised (contrast the secret checks in
    # core/config.py, where serving would mean writing unreadable PHI).
    if not init_error_tracking(settings) and settings.is_production:
        log_event(
            logger,
            "observability.error_tracking.missing_in_production",
            level=logging.ERROR,
            reason="no_dsn",
        )

    if settings.s3_provision_bucket_on_startup:
        from app.services.storage import ensure_bucket_configured

        try:
            ensure_bucket_configured()
        except Exception:  # noqa: BLE001 - startup must not crash over bucket admin permissions
            logger.warning("Could not verify/configure the S3 bucket at startup", exc_info=True)
    yield


app = FastAPI(
    title="Remedy Scribe API",
    version="0.1.0",
    lifespan=lifespan,
    # Phase 4.1: no interactive docs in production. Swagger UI is the only
    # HTML this API serves, it is the one page the strict CSP has to be
    # relaxed for, and it publishes a complete map of the PHI endpoints to
    # anyone who reaches the host. None of that is worth keeping for an
    # audience of one clinic's own client.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

# Phase 2.1 (decision 0024). The retired mobile client had no origin and so
# never needed CORS; a browser client does, and its absence is a silent
# failure — the preflight is rejected and the request never reaches a route,
# so nothing appears in the API log at all.
#
# allow_credentials=True is required for the httpOnly refresh cookie to be
# sent at all, and it is mutually exclusive with allow_origins=["*"] per the
# CORS spec (browsers reject a wildcard on a credentialed request). Hence an
# explicit allow-list from settings rather than a wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Phase 5.2: a response header is invisible to a cross-origin browser
    # unless it is exposed. Without this the API stamps a correlation ID the
    # web client cannot read, which breaks the one thing a correlation ID is
    # for — a doctor's error report naming the same ID the server logged.
    #
    # Retry-After (added alongside the login rate-limit countdown) needs
    # the same treatment for the same reason: it is not on the fetch spec's
    # small CORS-safelisted header list, so without this line the web
    # client's `response.headers.get("retry-after")` silently returns null
    # on a cross-origin dev setup even though the server sent it.
    expose_headers=[CORRELATION_HEADER, "Retry-After"],
)

# Added before the correlation middleware, so it sits inside it and still
# stamps its headers on *every* response CORSMiddleware short-circuits (a
# rejected preflight is still a response leaving this API).
app.add_middleware(SecurityHeadersMiddleware, settings=settings)

# Phase 5.2 (P0-8). Added last, so it is the outermost of the three. That
# ordering is the point rather than an accident: a request logger mounted
# inside CORSMiddleware never sees the responses CORS short-circuits, which
# is exactly how a CORS misconfiguration becomes invisible — Phase 2.1's own
# note, "the preflight is rejected and the request never reaches a route, so
# nothing appears in the API log at all". Outermost also means the
# correlation ID is bound before any other middleware can log, and that an
# exception escaping the router is recorded on its way past.
app.add_middleware(CorrelationIdMiddleware)

app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(patients.router, prefix=API_PREFIX)
app.include_router(encounters.router, prefix=API_PREFIX)
app.include_router(uploads.router, prefix=API_PREFIX)
app.include_router(consent.router, prefix=API_PREFIX)
app.include_router(notes.router, prefix=API_PREFIX)
app.include_router(audit_logs.router, prefix=API_PREFIX)
app.include_router(pilot.router, prefix=API_PREFIX)
# Phase 5.2 leaves this line free deliberately: the liveness/readiness
# router belongs here, unprefixed, so a load balancer probe does not have to
# know the API version. Its paths are already in observability.py's
# `_QUIET_PATHS`, so a probe every few seconds does not drown the log —
# while a *failing* probe is still logged, which is the one line about a
# health check anybody ever wants.
app.include_router(health.router)
