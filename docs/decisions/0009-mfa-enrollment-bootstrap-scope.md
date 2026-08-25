# 0009 — MFA self-service enrollment only covers first-time setup

**Phase:** 0.3 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** `POST /auth/mfa/enroll` only succeeds when `mfa_secret` is not
already set. An already-enrolled clinician gets a `409`, not a fresh
secret. Re-enrollment (lost authenticator app, phone replaced) has no
self-service path yet — it needs an admin/support action this phase
doesn't build.

**Options considered:** (a) enrollment is one-time, self-service only for a
brand-new account, as chosen; (b) let `/mfa/enroll` always issue a new
pending secret, overwriting any existing enrollment, gated only by
password; (c) require an already-valid MFA code to re-enroll (prove the old
factor before replacing it).

**Why:** (b) turns "I know your password" into "I can silently replace your
MFA and lock out the real device," which is a worse position for an
attacker with a leaked password to be in than today's system (where they'd
at least need the *existing* TOTP secret too). (c) is the right long-term
answer but has a bootstrapping gap this phase doesn't resolve: it doesn't
help someone who has genuinely lost the authenticator, which is the actual
scenario "re-enrollment" exists for. (a) doesn't solve re-enrollment at all,
but it also doesn't make anything worse than the pre-existing seed-script-only
state, and it closes the self-service loophole rather than opening a new one.

**What would change my mind:** this is a real gap, not a deferred nice-to-
have — before pilot, decide who handles "doctor lost their phone and needs
MFA reset" operationally (likely: an admin-only reset endpoint that clears
`mfa_secret` and forces the normal first-time-enrollment path again, logged
to the audit trail). Worth doing alongside 0.3's `revoke-sessions` endpoint,
since it's the same operational moment for the same admin.
