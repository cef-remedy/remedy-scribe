"""Login rate limiting and account lockout (Phase 0.3).

Both read app/models/login_attempt.py — a plain append-only log of every
login attempt, success or failure — rather than maintaining a mutable
counter that has to be incremented, reset, and expired correctly. That
mirrors how this codebase already treats the consent ledger and audit
log: history you fold over beats state you mutate. One consequence
worth knowing: a lockout has no explicit "unlock" — it clears itself
the moment its oldest counted failure ages out of the window.

Two independent checks, both against the same table:

- Per-IP rate limit: caps raw request volume from one address,
  regardless of which email it's aimed at (stops flooding / credential
  spraying across many accounts from one source).
- Per-email lockout: caps failed attempts against one account,
  regardless of source IP (stops distributed guessing against a single
  target from many addresses).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.login_attempt import LoginAttempt


class RateLimitedError(Exception):
    """Too many requests from this IP address in the last minute."""


class AccountLockedError(Exception):
    """Too many failed attempts against this email in the lockout window."""


def check_login_rate_limit(db: Session, *, email: str, ip_address: str) -> None:
    """Raise before any password/MFA check runs, so a request that's
    going to be rejected never even touches credential verification —
    both for cost and so the rejection itself can't be used as a timing
    oracle on whether the account exists.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)

    ip_window_start = now - timedelta(minutes=1)
    ip_attempts = (
        db.query(LoginAttempt)
        .filter(LoginAttempt.ip_address == ip_address, LoginAttempt.created_at >= ip_window_start)
        .count()
    )
    if ip_attempts >= settings.login_rate_limit_per_ip_per_minute:
        raise RateLimitedError("Too many login attempts from this address. Try again in a minute.")

    lockout_window_start = now - timedelta(minutes=settings.login_lockout_window_minutes)
    failed_for_email = (
        db.query(LoginAttempt)
        .filter(
            LoginAttempt.email == email,
            LoginAttempt.successful.is_(False),
            LoginAttempt.created_at >= lockout_window_start,
        )
        .count()
    )
    if failed_for_email >= settings.login_lockout_threshold:
        raise AccountLockedError(
            f"Too many failed attempts for this account. Try again in "
            f"{settings.login_lockout_window_minutes} minutes."
        )


def record_login_attempt(db: Session, *, email: str, ip_address: str, successful: bool) -> None:
    db.add(LoginAttempt(email=email, ip_address=ip_address, successful=successful))
    db.commit()
