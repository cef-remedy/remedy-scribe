# PRD: Remedy Scribe — In-Clinic AI Consultation Note-Taker

**Author:** fec (Product) · **Date:** August 18, 2026 · **Status:** Draft v1

*This PRD consolidates decisions already made in the project's research artifacts (`product-plan-v0.1.md`, the Notion proposal, and the model-selection proposal) into a single spec formatted for engineering handoff and roadmap planning. It does not re-derive the legal research or vendor comparisons — those are referenced, not repeated.*

---

## Problem Statement

Remedy's doctors document in-person consultations on paper, while Remedy's existing AI note-taker only covers online consultations — a gap that widens with every clinic added, since paper doesn't scale, doesn't produce a searchable patient history, and gives neither the doctor nor the patient an organized record of what was discussed. Every consultation currently ends with a doctor writing by hand instead of engaging with the next patient, and every prior visit's context lives only in whatever the doctor remembers or can find in a paper chart. Left unsolved, this caps how many clinics and doctors Remedy can operate without documentation quality degrading, and leaves patient records exactly as fragmented as they are today.

## Goals

| Goal | Type | Measurable target |
|---|---|---|
| Notes are complete and correctly organized under the right patient | User | ≥98% of pilot consultations produce a signed note correctly linked to the right patient |
| Doctors trust the AI draft enough to keep using it | User | ≥60% of pilot doctors are still using the app voluntarily in week 4 (industry benchmark for comparable tools) |
| The AI draft is accurate enough to be a time-saver, not a liability | User | ≥70% of signed notes require only minor edits from the doctor |
| Documentation time drops relative to paper | User/Business | Measured against a week-0 paper baseline; no target promised until that baseline exists (see Open Questions) |
| The AI pipeline stays cheap enough to be a non-issue | Business | Combined transcription + note-generation cost stays under $0.10 per consult (currently modeled at $0.056–$0.067) |

## Non-Goals

| Non-goal | Why it's out of scope |
|---|---|
| Prescriptions | Governing FDA circulars' current legal status is unverified, and controlled substances require a physical form that cannot be replaced electronically. Excluded entirely, not deferred. |
| External EMR/HIS integration | v1 is a standalone system; Remedy operates all its own clinics, so there's no integration partner to build against yet. |
| Diagnostic suggestion, triage, or automated coding | Beyond the safety risk of an unproven Taglish pipeline making clinical calls, this is the category of feature most likely to push the product into the "high-risk" tier of pending Philippine AI legislation. |
| Real-time/streaming transcription | Offline-first architecture implies batch processing, which is also 2–4x cheaper. Streaming solves a problem (live feedback) that isn't on the current requirements list. |
| Patient-facing app or portal | Patient outputs (Section: Requirements) are delivered by the doctor at the point of care, not through a self-serve portal — no separate patient-facing surface to build in v1. |
| Multi-tenant configurability | Remedy operates all clinics in scope; building for one tenant now and generalizing later, if ever offered externally, is the cheaper sequencing. |
| Per-doctor custom note templates | One well-designed template ships first. This will be requested during the pilot — it's deferred deliberately, not overlooked. |

## User Stories

### Doctor (primary persona)

- As a doctor, I want to start recording a consultation with one tap so that I don't disrupt the flow of the visit.
- As a doctor, I want the app to capture consent from everyone present before recording begins so that I'm not personally exposed to legal risk.
- As a doctor, I want to be prompted to pause and re-consent if someone new joins mid-visit so that I stay compliant without having to remember the rule myself.
- As a doctor, I want a structured draft note ready within a couple of minutes of ending the consultation so that I can review it before the next patient sits down.
- As a doctor, I want the draft to lead with Assessment and Plan rather than a wall of transcript-like text so that I can verify the most clinically important content first.
- As a doctor, I want to tap any line in the note and see the transcript passage — and hear the underlying audio — it came from so that I can verify anything that looks off before signing.
- As a doctor, I want a way to add exam findings that were never spoken aloud so that the Objective section isn't left empty or invented.
- As a doctor, I want the app to keep recording and queue the upload when there's no signal so that I never lose a consultation to a dropped connection.
- As a doctor, I want to see a visible queue of recordings still waiting to upload so that I know nothing has silently failed.
- As a doctor, I want to select or create a patient record by name when I start a session so that the note files under the right patient's history.
- As a doctor, I want to see a holding area for any recording I started before selecting a patient so that nothing gets lost if I hit record first and named the patient later.
- As a doctor, I want to see the prior visit's assessment and plan while reviewing a new note so that I have longitudinal context without digging through old records.
- As a doctor, I want signing a note to be a distinct, deliberate action tied to my name and PRC license number so that the legal record clearly shows I reviewed and adopted it.

### Patient (secondary persona)

- As a patient, I want to be clearly and verbally asked for my consent before being recorded so that I understand and control how my information is used.
- As a patient, I want to decline being recorded without it affecting my care so that I don't feel pressured to agree.
- As a patient, I want to withdraw my consent at any point so that I retain control over my own data.
- As a patient, I want to receive a plain-language summary of my visit in Filipino, if I ask for one, so that I actually understand my diagnosis and next steps.

### Compliance & Admin

- As a compliance officer, I want an immutable log of every consent given, declined, or withdrawn so that Remedy can demonstrate compliance if audited.
- As a compliance officer, I want audio retention duration to be a configurable value, not a hardcoded default, so that Remedy's data handling can match its published privacy policy.

### Edge cases

- As a doctor, I want a clear, specific error state if note generation fails so that I know to retry or fall back to writing the note manually — not a silent gap in the record.
- As a doctor, I want the app to work exactly the same way if I decline to record so that opting out doesn't mean opting out of the tool entirely.

---

## Requirements

### P0 — Must-Have (v1 cannot ship without these)

**1. Consent-gated recording**
- Given a doctor taps "Record," when no consent record exists for that encounter, then the app blocks recording and presents the consent script (Filipino + English) before anything is captured.
- Given consent is given, when recording starts, then the spoken exchange is captured as the first segment of the audio file and a persistent recording indicator remains visible for the duration.
- Given a new participant joins mid-recording, when the doctor flags this (or the app detects a new speaker), then recording pauses until fresh consent is logged.
- Given a patient withdraws consent, when the withdrawal is submitted, then processing stops and the associated audio is queued for deletion without undue delay.
- [ ] Immutable, append-only consent ledger records participant roster, purposes consented to, timestamp, and language of the script for every encounter.

**2. Offline-first capture and upload**
- [ ] Audio is recorded and encrypted on-device before any network activity is required.
- [ ] Uploads are resumable and chunked, with an idempotency key that prevents duplicate notes from a retried upload.
- [ ] The doctor sees a visible, persistent queue status for any recording not yet uploaded.
- [ ] Local audio is deleted only after the server confirms receipt and note generation has begun.
- [ ] Recording survives interruption (incoming call, app backgrounded, device locked) without data loss.

**3. Speech-to-text transcription**
- [ ] Integrates ElevenLabs Scribe v2 (see model-selection proposal) with speaker diarization enabled.
- [ ] Transcript preserves Filipino speech verbatim — no silent translation to English.
- [ ] Word-level confidence is retained and passed to the note-generation step.

**4. AI note generation**
- [ ] Single fused call using GPT-5.6 Luna, committed to directly without a formal vendor bake-off (see Open Questions), transforms the transcript into a structured note. Claude Haiku 4.5 remains available as a configured fallback if Luna underperforms in practice.
- [ ] Note is ordered Assessment → Plan → Subjective → Objective.
- [ ] Generation is suppressed (not filled with inferred content) over silent or low-confidence audio windows.
- [ ] Output defaults to hedged clinical language rather than flat certainty.
- [ ] Every generated line is traceable back to its source transcript passage.

**5. Review, edit, and signing workflow**
- [ ] Note state machine enforces four distinct states: generated → filed → authenticated → signed, with no state skippable.
- [ ] Signing captures doctor identity, PRC license number, and timestamp in an audit trail.
- [ ] Doctor can freely edit any section before signing; edits are tracked for the edit-burden metric (Success Metrics).

**6. Patient identity and linking**
- [ ] Starting a session accepts a typed or dictated patient name and fuzzy-matches against the existing directory (exact match links silently; near match requires one-tap confirmation; no match creates a new record with name + birthdate).
- [ ] Recording is never blocked on identity — an unlinked session lands in a persistent "loose sessions" tray with a one-tap linking action.
- [ ] Deduplication uses name + birthdate together, not name alone.
- [ ] Patient identity is re-confirmed at the moment a note is filed, not only at recording start.

**7. Grounding UI**
- [ ] Tapping a line in the note highlights the transcript passage it came from.
- [ ] Tapping again plays the underlying audio at that point in the recording.

**8. Security baseline**
- [ ] Encryption in transit and at rest.
- [ ] Multi-factor authentication for clinician access.
- [ ] Role-based access control on a need-to-know basis.
- [ ] Access and change logs retained and reviewable.

### P1 — Nice-to-Have (strong fast-follow candidates)

- [ ] Patient-facing plain-language visit summary (Filipino, 6th-grade reading level), generated only on patient request, delivered before the patient leaves the room.
- [ ] Referral letter drafting, physician-reviewed and signed before release.
- [ ] Automatic injection of the prior visit's Assessment and Plan into the note-generation prompt as labeled historical context.
- [ ] Front-desk check-in queue integration, so the doctor taps a name from today's checked-in list instead of typing it.
- [ ] Dermatology-specific quick-entry pad for Objective findings (lesion location/size, Fitzpatrick type, common findings) as a narration alternative.

### P2 — Future Considerations (not built now, but shouldn't be architected out)

- [ ] Medical certificate generation (post-procedure clearance, fit-to-work), physician-signed only, once legal template requirements are finalized.
- [ ] Per-doctor custom note templates.
- [ ] Multi-tenant configurability, if the product is ever offered outside Remedy's own clinics.
- [ ] Automated data-portability export (self-service).
- [ ] Self-serve retention-settings UI (v1 ships retention as a config value, not a screen).
- [ ] External EMR/HIS integration.

---

## Success Metrics

**Leading indicators**
- **Edit burden:** % of signed notes requiring only minor edits, and median edit distance between draft and signed version. Target: ≥70% minor-edit-only within the pilot. Measured continuously from week 4 (internal alpha) onward.
- **Correctly-filed rate:** % of consultations resulting in a complete note linked to the right patient. Target: ≥98%. Measured weekly.
- **ASR/note-generation bake-off pass rate:** clinically-weighted entity error rate (drug names, doses, diagnoses, negation), scored separately for patient-side and doctor-side speech. Target and pass/fail threshold to be set once baseline bake-off data exists (week 1–2).

**Lagging indicators**
- **Voluntary use:** % of pilot doctors still using the app without prompting in week 4. Target: ≥60% (industry benchmark for comparable tools).
- **Documentation time saved:** minutes per consult vs. the week-0 paper baseline. Tracked and reported; no target promised until the baseline exists — published results for comparable products range from 18 seconds to 5.6 minutes, so this is deliberately not a headline commitment.
- **Unsafe acceptance rate:** % of signed notes later found to contain an incorrect clinical element, sampled via weekly manual review plus a five-star rating prompt after each encounter. Target: below the 7% one-or-two-star tail observed in comparable published pilots.

---

## Open Questions

| Question | Owner | Blocking? |
|---|---|---|
| Does ElevenLabs' BAA/DPA actually cover the Scribe API (not just their Agents product)? | Legal | Yes — could reverse the ASR recommendation |
| Is the RA 4200 consent flow (Requirement P0-1) cleared by Philippine counsel? | Legal | Yes — recording feature cannot launch without this |
| What is the default audio-retention period? | Legal/Compliance | Yes — affects Requirement P0-2 and the consent ledger design |
| Does GPT-5.6 Luna hold up on Taglish-specific note quality (hedging, negation, Filipino preservation)? A formal vendor bake-off was dropped — no practical way to source enough consented test recordings — so this is unresolved going into the build, not before it. | Engineering | No — monitored via the edit-burden metric and doctor feedback starting at internal alpha, rather than resolved upfront. Claude Haiku 4.5 stays wired in as a configured fallback if Luna falls short. |
| What devices do doctors actually carry, and does a phone fit the physical workflow? | Design/Engineering | No — validated cheaply in week 0 shadowing, not a launch blocker but shapes the build |
| Are pediatric patients in scope for the pilot? | Stakeholder | No — affects consent obligations (NPC Advisory 2024-03) if yes, doesn't block a v1 aimed at adult consultations |
| Can this system share code, prompts, or note format with the existing online-consult notetaker, or is that a separate third-party tool? | Stakeholder/Engineering | No — affects build efficiency, not scope |
| Is there a standard paper note template across doctors today? | Design | No — shapes the note template design; sample photos requested but not yet received |

---

## Timeline Considerations

- **Hard constraint:** lean team, MVP target of 4–8 weeks.
- **Accepted risk, not a blocking dependency:** the ASR + note-generation vendor bake-off was dropped — there's no practical way to source enough consented recordings to run one — so ElevenLabs Scribe v2 and GPT-5.6 Luna are committed to directly and validated during the build instead of before it. This compresses the timeline by roughly two weeks but means internal alpha (Week 2) is the first real check on both vendors, not a formal evaluation in Weeks 1–2.
- **Blocking dependency:** Requirement P0-1 (consent-gated recording) cannot ship to real patients until Legal confirms the RA 4200 flow — this does not block engineering from building everything else in parallel.
- **Suggested phasing**, updated from the full product plan's original 8-week roadmap to reflect the compressed timeline:
  - Week 0: Discovery, paper-baseline timing, vendor outreach
  - Week 1: Core pipeline (P0-3, P0-4), including offline recording validation as part of the build
  - Week 2: Patient identity, review/edit UI, signing (P0-5, P0-6) → internal alpha, the first real-world test of the ElevenLabs/Luna choice
  - Week 3: Grounding UI (P0-7)
  - Week 4: P1 patient-facing outputs, offline hardening
  - Week 5: Security baseline (P0-8), breach runbook
  - Week 6: Measure against baseline, go/no-go

A detailed week-by-week breakdown split by Legal vs. Engineering ownership already exists in `product-plan-v0.1.md`, Sections A.5 and B.8 — the roadmap that follows this PRD translates that into a Now/Next/Later view.
