# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Doctors (primary, and the only user this UI needs to serve well).** They operate the app themselves, on a clinic laptop, during and immediately after an in-person consultation with a patient physically in front of them. Their hands and attention are mostly on the patient, not the screen — recording has to start in one tap and disappear from their attention until the visit ends.

Two other roles exist in the data model (`compliance`, `admin`) and are enforced server-side via RBAC, but — confirmed by this redesign's own completeness audit — neither has ever had a UI surface built for it. `compliance` ("read and audit only") currently lands on the exact same doctor worklist as everyone else, with nowhere to go. This redesign builds a real surface for that role for the first time.

## Product Purpose

Remedy Scribe replaces paper documentation for in-person clinic consultations. A doctor records the consultation (with consent captured first, every time), and the app transcribes it, generates a structured draft note, and lets the doctor verify and sign it before it becomes the permanent record. It exists because paper doesn't scale past one clinic, produces no searchable history, and leaves every prior visit's context to whatever the doctor remembers.

## Positioning

The differentiating mechanism, not shared by a generic transcription tool: **grounding**. Every line of the generated note traces back to the exact transcript passage — and the exact moment of audio — it came from. A doctor can verify anything that looks off before signing, rather than trusting a black box. That verification loop, plus a deliberate PRC-license signing ceremony, is what makes the AI draft something a doctor can adopt as their own legal record rather than something they have to double-check from scratch.

Two other things a competitor claiming "AI note-taking" could not casually copy: the whole capture path is **offline-first** (a dropped clinic wifi connection never loses a consultation — it queues and retries), and consent is captured **in the room, verbally, in Filipino or English**, before the microphone is ever live — a legal requirement (RA 4200), not a nicety.

## Operating Context

- A clinic laptop, used between patients in a room with the patient present — not a phone, not a quiet office.
- Clinic wifi is assumed unreliable; recording must survive a dropped connection without losing the consultation.
- Consultations are conducted in Taglish (mixed Filipino/English); the consent script itself is bilingual and the language actually spoken is logged.
- A consultation is a bounded, repeated ritual: consent → record → (background: transcribe, draft) → review → sign, several times a day, every working day. The UI is used far more than it is looked at for the first time — familiarity and speed compound; novelty does not.
- Signing is a distinct, deliberate, legally-weighted action tied to the doctor's name and PRC license number — not an autosave.

## Capabilities and Constraints

**Built and working (backend confirmed complete; this redesign covers the UI for all of it):**
- Consent capture (bilingual script, roster, decline, mid-visit re-consent, withdrawal with real deletion)
- Recording (offline-first, encrypted local storage, gap detection, queued upload with backoff)
- Real ASR + note generation (Groq), producing an APSO-structured draft
- Grounding (tap a note line → see the transcript passage → hear the audio moment)
- Patient identity (name-first fuzzy search, loose-session linking for a recording started before a patient was picked, prior-visit context)
- Review, per-line editing, and a deliberate signing ceremony
- Retention enforcement, audit logging, and PHI encryption (server-side; not directly visible in the UI, but the UI must not undermine them — e.g. audio never cached client-side beyond what P0-2 already allows)

**Confirmed missing, and in scope for this pass (found by this redesign's own audit, not previously tracked as a gap):**
- MFA enrollment — `POST /auth/mfa/enroll` exists server-side and is fully typed on the client, but no screen has ever called it. Today the only way any account gets MFA is a seed script writing a secret directly into the database.
- A compliance/audit view — the `compliance` role has RBAC access to `GET /audit-logs/access-report` and exists as a real seeded account, but no screen has ever been built for it.
- A retry action on failed encounters — the "Needs attention" list shows a failed pipeline_status but nothing calls `POST /encounters/{id}/retry`; a doctor sees the failure with no recourse in the app.

**Explicit non-goals (from the PRD, still standing):** prescriptions, external EMR/HIS integration, diagnostic suggestion or triage, real-time/streaming transcription, a separate patient-facing app, multi-tenant configurability, per-doctor custom templates. Do not design toward any of these.

**Legal constraint that is not yet resolved:** Philippine counsel has not yet cleared the RA 4200 consent script wording. The consent flow's mechanism is complete and tested; the exact script text is still provisional. Don't treat the current script copy as final, locked language.

## Brand Commitments

None locked. The product name "Remedy Scribe" is fixed (used in the PRD, the page title, and throughout the codebase) and should be treated as fixed. Everything visual — the current teal accent, the system-font stack, the card-based layout — was never a deliberate design decision; the codebase's own comment on this states so outright ("Minimal, legible, theme-aware... real design work belongs with the recording and review screens"). Confirmed with the product owner: fully free to replace.

## Evidence on Hand

- A real, running backend (FastAPI) and a real, running deployment (Render + Neon + Upstash + Google Drive), currently serving synthetic seeded data — no real patient has ever used this system.
- Seeded accounts for all three roles: `doctor@staging.remedy.example`, `compliance@staging.remedy.example`, `admin@staging.remedy.example` (synthetic, `.example` domain, not real people).
- Seeded encounters spanning every pipeline state the UI needs to render: signed, filed, generated, withdrawn, expired, blocked-on-consent, failed. No real testimonials, case studies, or press exist — none should be fabricated.
- No existing logo or icon beyond a generic placeholder favicon (`icon.svg`).

## Product Principles

1. **The doctor's attention belongs to the patient, not the screen.** Every screen this redesign touches should assume it's being glanced at, not read — one tap to start, unmistakable state at a glance, nothing that demands sustained attention mid-consultation.
2. **Trust is earned by verifiability, not by confidence.** The grounding mechanism is the product's actual differentiator; the design should make "check this before you sign it" feel like the natural, fast thing to do, not a chore bolted onto a review screen.
3. **A consultation is a repeated ritual, not a first impression.** Optimize for the hundredth use, not the demo — speed and predictability outrank novelty.
4. **Signing is legally weighted and must feel that way — once.** The moment of signing should read as deliberate and distinct from every editing action before it, without turning every other interaction into false ceremony.
5. **Every declared role gets a real surface.** A role enforced server-side (`compliance`) but invisible in the UI is a broken product for that role, not a smaller version of the doctor's product.

## Accessibility & Inclusion

No product-specific accessibility requirement has been separately established beyond ordinary web accessibility practice. Given the operating context (glanced at, not read, often in imperfect lighting between patients), legible contrast and unmistakable state at a glance are functional requirements here, not a nice-to-have — carry that into the visual world rather than treating it as a checklist item.
