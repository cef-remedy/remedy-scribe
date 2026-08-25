# 0004 — Who can read a note: authoring clinician only, or any clinician in the clinic?

**Phase:** 0.2 · **Decided by:** implementation · **Date:** 2026-08-25

**Decision:** `GET /notes/{id}` stays open to any authenticated, active
clinician regardless of role (`doctor`, `compliance`, `admin`) — no
`require_role` restriction, and no ownership check against the note's
encounter's `clinician_id`. Every read still goes through `audit.record`
(already wired before this phase), so need-to-know is enforced by making
access *accountable*, not by *blocking* it.

**Options considered:** (a) open to any clinician, accountable via audit log,
as implemented; (b) restrict to the authoring/encounter clinician only; (c)
restrict to the authoring clinician plus roles with an explicit review need
(`compliance`, `admin`), excluding other doctors.

**Why:** (b) breaks a real clinic workflow this system has to support —
colleague coverage, handoffs, and a doctor pulling up a patient's prior note
for context (P0's own "show the prior visit's assessment and plan for
longitudinal context" requirement in 2.6 assumes cross-clinician note access
is normal, not exceptional). (c) solves nothing (b) doesn't, since compliance
already needs read access for exactly the reasons in (a), and "other doctors"
being singled out as the untrusted group has no basis in how a small clinic
actually staffs. Write access is where the real risk sits — PHI is
disclosed on read either way, but *unauthorized modification* of a clinical
record is the more severe failure mode — so this phase's role restriction is
concentrated on `PATCH` and `transition` (doctor-only), while reads stay
observable-but-open, matching how EMRs conventionally scope clinic-wide
clinical access versus edit/sign authority.

**What would change my mind:** if Remedy's DPO or Philippine counsel requires
per-record read restriction (not just logging) as part of the RA 4200 /
Data Privacy Act compliance review mentioned in the roadmap, or if the pilot
surfaces a real case of a doctor browsing patients outside their own care
relationship — the audit log this decision leans on is exactly what would
surface that pattern, which is the point of building it before this
question comes up for real.
