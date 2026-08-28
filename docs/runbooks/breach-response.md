# Runbook — Personal data breach response

**Phase:** 4.3 (P0-8, roadmap Week 5) · **Status:** written, **never
exercised** · **Date:** 2026-08-28
**Related:** [0034](../decisions/0034-an-untested-control-is-a-hope.md),
[key-rotation](key-rotation.md), [backup-restore](backup-restore.md),
[0018](../decisions/0018-groq-whisper-instead-of-elevenlabs-scribe.md)
(the third-party ASR vendor)

> ⚠️ **This document is engineering's map of the system, not legal advice.**
> The regulatory summary in §2 was checked against public sources on
> 2026-08-28 and is cited; it has **not** been reviewed by counsel or by a
> Data Protection Officer. Two things must happen before this runbook is
> relied on in an actual incident: **Legal confirms §2**, and **a named DPO
> and breach response team exist** (§7). Both are flagged, not assumed.

## 1. Where the data is — the map you need in the first ten minutes

You cannot scope a breach without knowing what a given compromise exposes.
Every location holding personal or sensitive personal information:

| Location | What is there | Protection at rest | Notes |
|---|---|---|---|
| **Postgres** — `patients.full_name` | Patient names | Fernet, `EncryptedString` | Ciphertext without `PHI_ENCRYPTION_KEY` |
| **Postgres** — `transcripts.segments` | Verbatim consultation transcript | Fernet, `EncryptedJSON` | The most sensitive artifact in the system: everything said, including what the doctor chose not to write down |
| **Postgres** — `notes.{subjective,objective,assessment,plan}`, `note_revisions.{previous_text,new_text}` | Clinical note text and every edit | Fernet | |
| **Postgres** — `patients.birthdate` | Date of birth | **Plaintext** | Deliberate: it is the indexable dedupe key (P0-6). Name + birthdate is an identity pair |
| **Postgres** — `consent_ledger_entries.participant_roster` | JSON list of roles/names present at the consult | **Plaintext** | May contain patient or companion names |
| **Postgres** — `clinicians.{email,full_name,mfa_secret}` | Clinician identity and **TOTP seeds** | **Plaintext** | A database dump is enough to generate valid second factors. See §4 |
| **Postgres** — `audit_logs` | Who accessed what, when | No PHI by design | Append-only (Phase 4.2). This is the **evidence**, and §3 depends on it |
| **Object storage** (MinIO / S3) | Consultation audio | Bucket default encryption; presigned access only | Raw voice: identifiable, and sensitive personal information on its own |
| **Clinician's browser** (IndexedDB `remedy-scribe`) | Queued audio chunks awaiting upload | AES-GCM under a **non-extractable** `CryptoKey` | A lost or stolen laptop is a breach vector. `extractable: false` is enforced by the browser, not by hardware — see `apps/web/src/lib/recorder/crypto.ts` |
| **Groq** (third party) | Audio + resulting transcript, and note generation | Vendor-controlled | Decision 0018. Data **leaves the country**; a vendor breach is a breach of this system's data |
| **Anthropic** (third party) | Transcript text, where the earlier note path is configured | Vendor-controlled | |
| **Backups** | Everything in the first block | Whatever §2 of [backup-restore](backup-restore.md) applied | PHI a patient asked to delete still exists here |

**Two facts to have ready before you need them:**

1. **The `PHI_ENCRYPTION_KEY` is the difference between a database
   incident and a PHI disclosure.** A stolen dump without the key is a
   pile of ciphertext. The same dump with the key is every patient record
   in the clinic. This is why the two-artifact rule in
   [backup-restore](backup-restore.md) §1 exists, and it is the first
   question to answer in triage.
2. **The audit log is the only instrument that can scope a disclosure.**
   Phase 4.2 makes it append-only, so it can also be trusted afterwards.
   If it is missing rows, everything below becomes guesswork.

## 2. The legal clock  ⚠️ **needs Legal/DPO confirmation**

The Philippine **Data Privacy Act of 2012 (RA 10173)** and **NPC Circular
16-03 (Personal Data Breach Management)** govern. Health information is
**sensitive personal information** under the DPA, so essentially every
breach of this system's PHI is in the notifiable category.

Verified from public sources on 2026-08-28:

- **72 hours** to notify **both** the National Privacy Commission **and**
  the affected data subjects, running from *knowledge of, or reasonable
  belief in,* a notifiable breach — not from confirmation, and not from
  the breach itself.
- Notification is **mandatory when three conditions concur**: (a) the data
  involved is sensitive personal information, or information that could be
  used to enable identity fraud; **and** (b) there is reasonable belief it
  was acquired by an unauthorised person; **and** (c) the breach is likely
  to give rise to a **real risk of serious harm** to the data subject.
- **No delay is permitted** where at least **100 data subjects** are
  affected, or where disclosure of sensitive personal information will
  harm or adversely affect a data subject.
- A **full report** follows within **five (5) days**, unless the NPC grants
  an extension.
- Submission goes through the NPC's **Data Breach Notification Management
  System (DBNMS)**; NPC Advisory 2026-02 clarifies the submission process.
- Annual security incident reports are separately required.
- **Concealing** a breach after knowing of the notification obligation is a
  criminal offence under the DPA, carrying imprisonment and a fine of
  ₱500,000–₱1,000,000.

**What could not be verified here, and what that means.** The primary NPC
documents (`privacy.gov.ph`) returned HTTP 403 to automated retrieval, so
the above comes from search-result summaries of NPC Circular 16-03 and
from DLA Piper's Philippines breach-notification page. **Section-level
citations, the current text of the circular, whether NPC Advisory 2026-02
changes any deadline, and whether anything issued after 16-03 supersedes
it, all need confirmation by counsel.** A public summary is enough to plan
against; it is not enough to rely on when a 72-hour criminal-liability
clock is running.

**The consequence for engineering, which is the point of putting this in a
runbook at all:** the clock starts at *reasonable belief*, not at
certainty. So the binding constraint on compliance is **detection**, not
notification — and **this system has no detection.** Phase 5.2 has not
been built: there is no alerting, no error tracking, no anomaly detection
on the audit log. Today a breach is noticed when a human notices. That is
the single largest gap in this runbook and it is not fixable inside 4.3.

## 3. Triage — the first hour

Assign one incident lead. Everything below is theirs to delegate, not to
do personally.

**3.1 Start a timeline, in UTC, in a document outside this system.** Every
entry gets a timestamp and a name. The 72-hour clock is defensible only if
you can show when you first had reasonable belief.

**3.2 Answer the five scoping questions, in this order.** They are ordered
by how much they change the answer:

1. **Is the `PHI_ENCRYPTION_KEY` in scope?** (Compromised host with the
   process environment? A backup taken alongside a `.env`? An attacker
   with code execution on the API?) If yes, treat all encrypted columns as
   disclosed in plaintext. If no, the encrypted columns are ciphertext and
   say so — precisely, and only if you can show the key was not reachable.
2. **Which data locations from §1 are in scope?** Postgres, object
   storage, a laptop, a vendor, a backup. Name them.
3. **Which data subjects?** Query the audit log, not your memory:
   `audit_logs` rows are `(clinician, action, entity_type, entity_id,
   created_at)` and hold no PHI, so they can be exported to an incident
   ticket safely. The `/api/v1/audit-logs` endpoint (compliance role) is
   the supported read path.
4. **How many?** The count matters legally — at 100 or more, no delay in
   notification is permitted (§2).
5. **Is there a real risk of serious harm?** For consultation transcripts
   and audio the honest default answer is **yes**. Argue otherwise only in
   writing, and only with the DPO.

**3.3 Preserve evidence before you fix anything.** Snapshot the database
(see [backup-restore](backup-restore.md) — restore into a *new* database,
never over the original), copy the relevant logs off the host, and do not
redeploy the affected version until an image or commit SHA is recorded.
Remediation destroys the evidence that scopes the incident.

## 4. Containment — what this system actually gives you

Each of these is a real capability in this codebase, with its blast radius
stated. Choose deliberately; several of them are visible to every clinician
in the clinic mid-consultation.

| Action | How | Effect |
|---|---|---|
| **Kill one clinician's sessions** | `app/services/refresh_tokens.py:revoke_all_for_clinician`, or the revoke-sessions endpoint | That clinician is logged out everywhere. Access tokens already issued survive up to `ACCESS_TOKEN_EXPIRE_MINUTES` (15) |
| **Disable an account** | `clinicians.is_active = false` | Login and refresh both refused |
| **Kill *every* session** | Rotate `JWT_SECRET` and restart | Every access token in existence becomes unverifiable immediately. The whole clinic is logged out. This is the big red button, and the right one if token theft is suspected |
| **Cut off object storage** | Rotate `S3_ACCESS_KEY` / `S3_SECRET_KEY` | New presigned URLs only. **Already-issued presigned URLs keep working until they expire** — 900 s for upload parts, 300 s for playback. Wait them out, or revoke at the bucket policy |
| **Re-key PHI** | [key-rotation](key-rotation.md) | Only meaningful if the *old* key leaked and the ciphertext did not. Read that runbook first; a half-finished rotation is its own incident |
| **Reset MFA** | Clear `clinicians.mfa_secret`, re-enrol | **Required if the database was disclosed.** TOTP seeds are stored in plaintext, so a dump is enough to generate valid second factors indefinitely |
| **Stop the pipeline** | Stop the Celery worker | Halts further transmission of audio and transcripts to Groq. Use if the vendor is the suspected source |

**Vendor breach (Groq, Anthropic).** Remedy remains the personal
information controller: the notification obligation is Remedy's, whether
or not the vendor tells anyone. What is in scope is the audio and
transcript for every encounter processed in the affected window — derive
that list from `encounters.pipeline_updated_at` and the audit log, not
from the vendor's estimate. ⚠️ **Whether the vendor contract obliges them
to notify Remedy within a defined window is a Legal question and, as far
as this repository shows, an unanswered one.** Decision 0018 records that
a BAA/DPA was an open blocker at vendor-selection time. If no such clause
exists, Remedy cannot meet a 72-hour clock it does not control, and that
is a contract problem, not an engineering one.

**Lost or stolen clinician laptop.** Queued audio in IndexedDB is AES-GCM
encrypted under a non-extractable key, which is a genuine mitigation and
should be stated as one — but it is enforced by the browser, not by a
secure element, so someone with the device *and* the unlocked OS session
can still use the key. Treat an unlocked, un-encrypted-disk laptop as a
disclosure of every recording still queued on it. Revoke that clinician's
sessions immediately (row 1 above).

## 5. Notification

Draft with the DPO; do not send engineering's words to data subjects.

**To the NPC**, via DBNMS, within 72 hours. Per NPC Circular 16-03 the
notification must describe at minimum: the nature of the breach, the
sensitive personal information possibly involved, and the measures taken
to address it. In practice also include the timeline from §3.1, the
approximate number of affected data subjects, and contact details for the
DPO.

**To affected data subjects**, within the same 72 hours: what happened, in
plain language and in the language the consult was conducted in (the
consent script supports Filipino and English — `script_language`); what
information was involved; what Remedy has done; what the patient can do;
and how to reach the DPO. For a clinic pilot, expect this to be delivered
in person or by phone as well as in writing.

**Full report to the NPC within five days.**

## 6. After

- Write the post-incident review. It belongs next to this runbook.
- Update this document with what was wrong about it — the first real
  incident always finds something.
- Whatever detection would have caught it earlier is now a Phase 5.2
  requirement with a name attached, not a generic "add alerting" item.

## 7. Roles — **not yet filled**

| Role | Who | Status |
|---|---|---|
| Data Protection Officer | — | **Unassigned.** The DPA requires a designated DPO. Nothing in §2 or §5 can be executed without one |
| Incident lead | — | Unassigned |
| Breach response team | — | **Unassigned.** NPC Circular 16-03 requires one |
| Legal counsel (DPA) | — | Unassigned; owns confirming §2 |
| Vendor contacts (Groq, Anthropic) | — | Escalation path and contractual notification window both unknown |

A runbook with an empty roles table is a plan, not a capability. Filling
this table is the cheapest item in Phase 4.3 and the one that most changes
whether any of the rest of it happens.

## Sources

Checked 2026-08-28. The two `privacy.gov.ph` links are the primary sources
and are cited for the record, but returned HTTP 403 to automated retrieval
here — they were **not** read directly, and everything attributed to them
below came from search-result summaries. Confirm with counsel.

- [NPC Circular 16-03 — Personal Data Breach Management](https://privacy.gov.ph/wp-content/uploads/2022/01/sgd-npc-circular-16-03-personal-data-breach-management.pdf) (not retrieved; 403)
- [NPC — Breach Reporting](https://privacy.gov.ph/pips-and-pics/breach-reporting/) (not retrieved; 403)
- [DLA Piper — Breach notification in the Philippines](https://www.dlapiperdataprotection.com/?t=breach-notification&c=PH) (retrieved)
- [NPC — Republic Act 10173, Data Privacy Act of 2012](https://privacy.gov.ph/data-privacy-act/)
- [Digital Policy Alert — NPC Circular 16-03](https://digitalpolicyalert.org/event/23268-implemented-npc-circular-16-03-on-personal-data-breach-management)
