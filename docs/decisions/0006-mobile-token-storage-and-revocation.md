# 0006 — Where does the token live on the device, and what happens if the phone is lost?

**Phase:** 0.3 · **Decided by:** user · **Date:** 2026-08-25

**Decision:** `expo-secure-store` (Keychain/Keystore-backed) holds the refresh
token; the access token is kept in memory only and is short-lived (15
minutes — see 0007). Lost-phone handling is server-side revocation, not
"wait for expiry": an admin can revoke every refresh token for a clinician
(`POST /auth/clinicians/{id}/revoke-sessions`), which stops all future
silent renewal immediately.

**Options considered:** (a) secure-store for a refresh token + in-memory
access token, as chosen; (b) secure-store holding the access token directly,
long-lived, no refresh flow; (c) in-memory only for everything, forcing
re-login (password + MFA) on every app launch.

**Why:** (b) was the pre-existing state's failure mode — no way to revoke a
long-lived token short of waiting out its expiry, and the checklist's own
heads-up about doctors getting logged out mid-consultation traces directly
to having no refresh path. (c) is the most secure against device theft but
fails the actual clinic workflow — re-entering password and TOTP every time
the app is reopened during a shift is the kind of friction that kills
"voluntary use in week 4." (a) gets both: the access token that actually
travels with every API request is short-lived and never touches disk, while
the thing that IS persisted (the refresh token) is exactly the thing this
system can revoke on command.

**What would change my mind:** if Remedy's device policy ends up mandating
biometric re-auth on every app resume (not just cold launch) for compliance
reasons, the in-memory access token's lifetime stops mattering much and the
refresh token becomes the sole thing worth hardening further — at that point,
consider binding the refresh token to a specific device/installation ID so a
copied refresh token alone (without the device) still can't be used.
