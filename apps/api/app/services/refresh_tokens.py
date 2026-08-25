"""Refresh-token issuance, rotation, and revocation (Phase 0.3).

Access tokens are short-lived, stateless JWTs (app/core/security.py) —
fast to verify, and impossible to revoke early without a blacklist
lookup on every request. Refresh tokens are the opposite on purpose:
an opaque random value, stored only as a hash (like a password), backed
by a DB row that CAN be flipped to revoked instantly. That split is what
makes "sign out this doctor's lost phone" possible at all — you cannot
reach into someone's pocket and delete a JWT, but you can refuse to
ever hand it a new access token again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import generate_refresh_token, hash_refresh_token
from app.models.refresh_token import RefreshToken


class RefreshTokenInvalidError(Exception):
    """Covers unknown, expired, and already-revoked/rotated tokens under
    one error type. A legitimate client's remedy is identical either
    way — log in again — and distinguishing the cases in the response
    would hand an attacker a way to enumerate token state.
    """


def issue_refresh_token(db: Session, clinician_id: str) -> tuple[str, RefreshToken]:
    """Returns (raw_token, row). The raw value is handed to the client
    once and never stored — only its hash lives in `row.token_hash`.
    """
    settings = get_settings()
    raw = generate_refresh_token()
    row = RefreshToken(
        clinician_id=clinician_id,
        token_hash=hash_refresh_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.refresh_token_expire_hours),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return raw, row


def _aware(dt: datetime) -> datetime:
    """SQLite (the test DB) hands back naive datetimes even from a
    DateTime(timezone=True) column — it has no real timestamptz type,
    so SQLAlchemy's tz-awareness there is write-only. Every value this
    module writes is UTC, so a naive read is always UTC too; Postgres
    never needs this (it round-trips tz-aware values natively), but the
    helper is harmless there since an already-aware datetime returns
    unchanged. Same class of gap as docs/decisions calls out for the
    Postgres-only consent trigger — an SQLite-vs-Postgres divergence
    that only bites when you actually compare against `now()`.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def rotate_refresh_token(db: Session, raw_token: str) -> tuple[str, RefreshToken]:
    """Validate `raw_token`, retire it, and issue its replacement.

    Reuse of a token that was already rotated out (`replaced_by_id` set
    — meaning a *newer* token already exists for this session) is not a
    race a legitimate client can trigger, since a client always presents
    the newest token it was handed. That specific case is treated as
    evidence of theft: every live session for the clinician is revoked,
    not just this one. A token revoked for an ordinary reason (logout,
    an admin's revoke-all) has no successor — presenting it again after
    that is just a stale/expired credential, not a compromise signal,
    and must not cascade into revoking sessions it has nothing to do
    with.
    """
    row = db.query(RefreshToken).filter(RefreshToken.token_hash == hash_refresh_token(raw_token)).one_or_none()
    if row is None:
        raise RefreshTokenInvalidError("Unknown refresh token.")

    now = datetime.now(timezone.utc)
    if row.revoked_at is not None:
        if row.replaced_by_id is not None:
            revoke_all_for_clinician(db, row.clinician_id)
            raise RefreshTokenInvalidError(
                "Refresh token already used; all sessions for this clinician were revoked."
            )
        raise RefreshTokenInvalidError("Refresh token has been revoked.")

    if _aware(row.expires_at) <= now:
        raise RefreshTokenInvalidError("Refresh token expired.")

    new_raw, new_row = issue_refresh_token(db, row.clinician_id)
    row.revoked_at = now
    row.replaced_by_id = new_row.id
    db.add(row)
    db.commit()
    return new_raw, new_row


def revoke_refresh_token(db: Session, raw_token: str) -> None:
    """Single-session logout. Silently no-ops on an unknown/already-
    revoked token — logging out twice isn't an error worth reporting.
    """
    row = db.query(RefreshToken).filter(RefreshToken.token_hash == hash_refresh_token(raw_token)).one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        db.add(row)
        db.commit()


def revoke_all_for_clinician(db: Session, clinician_id: str) -> int:
    """The lost-phone path: kill every live refresh token this clinician
    holds, on any device. Returns the count revoked, for the caller to
    report/audit. Already-issued *access* tokens keep working until
    they naturally expire (at most access_token_expire_minutes) — see
    docs/decisions/0006 for why that residual window is accepted rather
    than closed with an access-token blacklist.
    """
    now = datetime.now(timezone.utc)
    rows = (
        db.query(RefreshToken)
        .filter(RefreshToken.clinician_id == clinician_id, RefreshToken.revoked_at.is_(None))
        .all()
    )
    for row in rows:
        row.revoked_at = now
        db.add(row)
    db.commit()
    return len(rows)
