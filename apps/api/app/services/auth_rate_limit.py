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

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.login_attempt import LoginAttempt


def _aware(dt: datetime) -> datetime:
    """SQLite (the test DB) hands back naive datetimes even from a
    DateTime(timezone=True) column. Same gap, same fix, as
    app/services/refresh_tokens.py's helper of the same name.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _seconds_until_stale(oldest_created_at: datetime, window: timedelta, now: datetime) -> int:
    """Seconds until `oldest_created_at` ages out of `window`.

    That instant is also the earliest a rate limit or lockout can
    possibly clear: both are sliding-window counts over this same table,
    and a blocked request never reaches `record_login_attempt` (the
    route raises before recording it), so the count can only shrink
    between now and then, never grow. Ceil'd so the UI never reports
    "0:00" a second before the server actually agrees.
    """
    remaining = (_aware(oldest_created_at) + window) - now
    return max(0, math.ceil(remaining.total_seconds()))


class RateLimitedError(Exception):
    """Too many requests from this IP address in the last minute."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AccountLockedError(Exception):
    """Too many failed attempts against this email in the lockout window."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def check_login_rate_limit(db: Session, *, email: str, ip_address: str) -> None:
    """Raise before any password/MFA check runs, so a request that's
    going to be rejected never even touches credential verification —
    both for cost and so the rejection itself can't be used as a timing
    oracle on whether the account exists.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)

    ip_window = timedelta(minutes=1)
    ip_window_start = now - ip_window
    ip_query = db.query(LoginAttempt).filter(
        LoginAttempt.ip_address == ip_address, LoginAttempt.created_at >= ip_window_start
    )
    if ip_query.count() >= settings.login_rate_limit_per_ip_per_minute:
        oldest = ip_query.order_by(LoginAttempt.created_at.asc()).first()
        assert oldest is not None  # the count just proved at least one row exists
        raise RateLimitedError(
            "Too many login attempts from this address. Try again in a minute.",
            retry_after_seconds=_seconds_until_stale(oldest.created_at, ip_window, now),
        )

    lockout_window = timedelta(minutes=settings.login_lockout_window_minutes)
    lockout_window_start = now - lockout_window
    email_query = db.query(LoginAttempt).filter(
        LoginAttempt.email == email,
        LoginAttempt.successful.is_(False),
        LoginAttempt.created_at >= lockout_window_start,
    )
    if email_query.count() >= settings.login_lockout_threshold:
        oldest = email_query.order_by(LoginAttempt.created_at.asc()).first()
        assert oldest is not None  # same reasoning as above
        raise AccountLockedError(
            f"Too many failed attempts for this account. Try again in "
            f"{settings.login_lockout_window_minutes} minutes.",
            retry_after_seconds=_seconds_until_stale(oldest.created_at, lockout_window, now),
        )


def record_login_attempt(db: Session, *, email: str, ip_address: str, successful: bool) -> None:
    db.add(LoginAttempt(email=email, ip_address=ip_address, successful=successful))
    db.commit()
