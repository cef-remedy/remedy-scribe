# 0007 — Access/refresh token lifetimes, and what counts as "reuse"

**Phase:** 0.3 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** access tokens (stateless JWT) expire in 15 minutes; refresh
tokens (opaque, DB-backed, hashed at rest) expire in 12 hours and rotate on
every use. Presenting a refresh token whose `replaced_by_id` is already set
— i.e. a *successor already exists* — revokes every live session for that
clinician, on the theory that a legitimate client never replays a token it
has already exchanged. Presenting a token that's merely revoked-with-no-
successor (logout, an admin's revoke-all) is just rejected, with no cascade.

**Options considered — lifetimes:** (a) 15 min access / 12 hr refresh, as
chosen; (b) keep 30 min access, no refresh token (the pre-existing state);
(c) 1 hour access / 12 hr refresh. **Options considered — reuse:** (a)
distinguish "rotated-out" from "plainly revoked," as chosen; (b) treat any
presentation of a revoked token, for any reason, as compromise and nuke all
sessions.

**Why — lifetimes:** since the refresh flow makes renewal invisible to the
doctor, the access token's exact TTL no longer trades off against
mid-consult logout risk the way it did in (b) — so there's no UX reason to
prefer (c)'s longer window, and a shorter-lived token that's already
useless if a debug log or crash report ever captured one is the safer
default at effectively no cost. Twelve hours on the refresh token covers a
full clinic shift without requiring a fresh password+MFA each morning.
**Why — reuse:** (b) was tried first and failed its own test — logging out
device A must not silently kill device B's session, but "any revoked token
presented again is compromise" can't tell a clean logout from a stolen
token replay, because both leave `revoked_at` set. `replaced_by_id` is the
one signal that's only ever set by rotation, so it's the correct trigger.

**What would change my mind:** if alpha usage shows clinic wifi is flaky
enough that silent refresh calls fail often mid-shift (leaving a doctor
stuck needing to re-login), extend the refresh token lifetime or add
offline-tolerant retry on the client before touching the access token TTL —
that's a connectivity problem, not a token-lifetime problem.
