# Implementation Roadmap: Remedy Scribe — In-Clinic AI Note-Taker

**Format:** Now / Next / Later · **Based on:** `remedy-scribe-prd.md` · **Date:** August 18, 2026 · **Status:** Initial roadmap — no prior version to diff against

---

## Status Overview

Nothing has started yet. Four items in **Now** — discovery and legal groundwork. The ASR + note-generation vendor bake-off and the offline recording technical spike have been removed from this roadmap: there's no practical way to source enough consented recordings to run a formal bake-off, so the team is committing directly to **ElevenLabs Scribe v2** (ASR) and **GPT-5.6 Luna** (note generation) and proceeding straight to the engineering pipeline. This is a deliberate, accepted risk, not an oversight — see Risks and Dependencies below for how it's being monitored instead of pre-validated. The remaining go/no-go gate is the RA 4200 consent-flow review, which blocks the consent-gated recording feature from going live to real patients, though not from being built.

---

## Now (Weeks 0–2)

Foundational and de-risking work. High confidence in scope, because almost none of it is speculative — it's either measurement, vendor validation, or legal groundwork that has to happen before anything downstream can be built with confidence.

| Item | Description | Owner | Target | Status | Key Dependency |
|---|---|---|---|---|---|
| Discovery & paper baseline | Shadow doctors during real consultations, time current paper-based documentation, collect sample note templates | Product/Design | Week 0 | Not Started | None — can start immediately |
| DPO confirmation | Confirm or appoint Remedy's Data Protection Officer | Legal/Compliance | Week 0 | Not Started | None |
| RA 4200 consent-flow legal review | Philippine counsel review of the consent-gate design (participant roster, audible + on-screen consent, ledger, withdrawal) | Legal | Week 0, ongoing | Not Started | **Blocks:** consent-gated recording going live to real patients (Next) |
| ElevenLabs BAA/DPA confirmation | Confirm directly with ElevenLabs sales whether their BAA covers the Scribe API specifically, not just their Agents product | Legal/Procurement | Week 0–1 | Not Started | **Could reverse** the ASR vendor choice if it comes back negative |

**Removed from Now:** the ASR + note-generation vendor bake-off and the offline recording technical spike were both dropped — there isn't enough leverage right now to source the 25–30 consented recordings a bake-off needs. ElevenLabs Scribe v2 and GPT-5.6 Luna are committed to directly, and offline recording behavior gets validated as part of the pipeline build in Next rather than as a standalone spike beforehand. See Risks and Dependencies for how this is monitored.

---

## Next (Weeks 3–7)

The P0 requirement set from the PRD. Good confidence in *what* gets built — the requirements are specified and reviewed — less confidence in exact dates, since everything here is contingent on the Now-phase bake-off and legal review landing on schedule.

| Item | Description | Owner | Target | Status | Key Dependency |
|---|---|---|---|---|---|
| Core transcription-to-note pipeline | ASR integration (ElevenLabs Scribe v2) + fused correction/note-generation call (GPT-5.6 Luna), producing an APSO-structured draft with silence suppression. Offline recording behavior (background capture, interruption handling, encrypted local storage) is validated as part of this build, not as a separate upfront spike. | Engineering | Week 1 | Not Started | Committed vendors, no bake-off gate — see Risks |
| Patient identity & linking | Fuzzy-match patient directory, create-on-fly, loose-sessions tray, dedupe on name + birthdate | Engineering | Week 2 | Not Started | None beyond core pipeline |
| Review, edit & signing workflow | Four-state note lifecycle (generated → filed → authenticated → signed), doctor edit tracking, PRC-number signing | Engineering | Week 2 | Not Started | None beyond core pipeline |
| → Internal alpha | First real-consult test with 2–3 pilot doctors — **this is now the first real-world check on ElevenLabs Scribe v2 and GPT-5.6 Luna**, since no formal bake-off ran beforehand | Product/Engineering | End of Week 2 | Not Started | Requires the three items above |
| Grounding UI | Tap a note line → see transcript passage → play underlying audio | Engineering | Week 3 | Not Started | Depends on pipeline + review UI |
| Consent-gated recording — code complete | Full build of the P0-1 requirement (roster capture, audible + on-screen consent, ledger, withdrawal, pause/re-consent) | Engineering | Week 3–4 | Not Started | **Cannot go live to real patients until RA 4200 review clears** — build proceeds regardless |
| → Expanded pilot | Grow to 5–8 doctors | Product | Week 4 | Not Started | Requires grounding UI + consent gate live |
| Security baseline | Encryption, MFA, RBAC, access/change logs | Engineering | Week 5 | Not Started | None beyond core build |
| → Go/no-go checkpoint | Measure edit burden, correctly-filed rate, and voluntary use against the week-0 paper baseline | Product | Week 6 | Not Started | **This is a decision point, not a ship date** — wider rollout is contingent on this, not scheduled unconditionally |

---

## Later (Post-pilot, contingent on the Week 8 checkpoint)

Directional. These are the PRD's P1 fast-follows and P2 parking-lot items — scoped and understood, but timing depends entirely on the pilot succeeding.

**Likely fast-follows if the pilot passes (P1):**
- Patient-facing plain-language visit summary (opt-in, Filipino, 6th-grade reading level)
- Referral letter drafting
- Prior-visit context auto-injected into the note-generation prompt
- Front-desk check-in queue integration
- Dermatology-specific quick-entry pad for Objective findings

**Parking lot — not built now, architecture should not preclude them later (P2):**
- Medical certificate generation
- Per-doctor custom note templates
- Multi-tenant configurability
- Automated data-portability export
- Self-serve retention-settings UI
- External EMR/HIS integration

---

## Risks and Dependencies

| Risk / Dependency | Impact if it slips or resolves badly | Mitigation |
|---|---|---|
| **RA 4200 consent-flow review (Legal)** | Consent-gated recording — the entire product's core interaction — cannot go live to real patients without this. | Kicked off in Week 0, runs in parallel with engineering; engineering keeps building against the assumption it clears, but the launch gate is real. |
| **ElevenLabs BAA/DPA confirmation (Legal)** | If it comes back negative, the ASR vendor recommendation reverses to Speechmatics, which has weaker documented Taglish accuracy evidence. | Raised directly with ElevenLabs sales in Week 0–1, before the bake-off locks in a vendor choice. |
| **No pre-build validation of ElevenLabs Scribe v2 / GPT-5.6 Luna (accepted risk)** | The bake-off was dropped for lack of leverage to source consented test recordings, so the open question research kept raising — no vendor publishes a real Taglish/Filipino accuracy number — is now unresolved *before* the pipeline is built around these two vendors, not resolved before committing to them. If either underperforms, that surfaces mid-build or during internal alpha instead of in week 1–2, and any swap happens after real engineering time is already sunk. | Keep the note-generation model behind a configuration flag (Claude Haiku 4.5 is already the designed fallback) so a swap doesn't require a rebuild. Treat the Week 2 internal alpha as the de facto first real-world test — watch the edit-burden metric and doctor feedback closely from day one rather than waiting for the Week 6 checkpoint to notice a problem. If patient-side transcription looks materially worse than doctor-side in early alpha feedback, treat that as the same asymmetric-error-propagation risk the original research flagged, not a coincidence. |
| **Week 8 go/no-go checkpoint** | Wider rollout could stall if edit burden, correctly-filed rate, or voluntary use miss target. | Treated as a real decision point in this roadmap, not a formality — Later-phase items are explicitly contingent on it. |
| **Lean team capacity** | The Now + Next lists alone are a full 8-week load for a lean team; nothing in Later should be pulled forward without something else moving out. | P1/P2 items are deliberately excluded from Now/Next scope; any request to pull one forward should come with an explicit trade-off discussion. |

---

## Changes This Update

- **Removed:** ASR + note-generation vendor bake-off, and the offline recording technical spike — no practical way to source the consented recordings a formal bake-off requires.
- **Decision:** committing directly to ElevenLabs Scribe v2 (ASR) and GPT-5.6 Luna (note generation) without pre-build validation; Claude Haiku 4.5 remains the configured fallback.
- **Timeline shift:** removing ~2 weeks of upfront de-risking work pulls every subsequent Next-phase item earlier by two weeks (pipeline build now starts Week 1, go/no-go checkpoint now Week 6 instead of Week 8).
- **Added:** a new risk row covering the accepted trade-off of skipping vendor validation, with internal alpha reframed as the first real-world test of this choice.
