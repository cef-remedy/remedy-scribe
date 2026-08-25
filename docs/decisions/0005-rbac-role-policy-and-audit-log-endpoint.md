# 0005 — Per-route role policy, and adding a minimal audit-log endpoint early

**Phase:** 0.2 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** every clinical-workflow write route (`POST /consent`,
`POST /encounters`, `GET /encounters/loose`, `POST /encounters/{id}/link-patient`,
`POST /encounters/{id}/confirm-upload`, `POST /patients/match`, `POST /patients`,
`PATCH /notes/{id}`, `POST /notes/{id}/transition`) now requires role
`"doctor"` specifically — not `"admin"`. Also added `GET /audit-logs`
(`app/api/routes/audit_logs.py`), restricted to `compliance`/`admin`, months
ahead of the full review interface Phase 4.2 specifies.

**Options considered — role scope:** (a) `require_role("doctor")` only, as
implemented; (b) `require_role("doctor", "admin")`, treating admin as a
superuser over all clinical actions. **Options considered — audit
endpoint:** (a) add a minimal list endpoint now, as implemented; (b) leave
audit logs with no read path until Phase 4.2; (c) build the fuller Phase
4.2 interface (filtering, pagination, retention-aware query) now instead of
a stub.

**Why:** (b) for role scope conflates two different kinds of privilege —
"can operate the system" (admin) and "can attest to clinical documentation"
(doctor, tied to a PRC license at signing). Letting admin sign notes would
mean a non-clinician account can produce a legally-signed medical record,
which is a worse failure mode than admin simply lacking a convenience.
For the audit endpoint: (b) makes 0.2's own required test
("a doctor token must not be able to read the audit log") untestable against
real HTTP behavior — there'd be nothing to 403 against — and the checklist
explicitly wants that test now, not deferred. (c) is Phase 4.2's job and
pulls in filtering/retention concerns this phase isn't trying to solve; (a)
is the smallest addition that makes the RBAC boundary real and testable
without pre-building Phase 4.2's actual deliverable.

**What would change my mind:** if a future role needs cross-cutting
administrative access to clinical actions (e.g., a real "admin can act on
behalf of a doctor" support workflow), that should be its own explicit
capability (impersonation with its own audit trail), not a blanket
`require_role("doctor", "admin")` — expanding the tuple quietly is the
wrong fix if that need shows up.
