<!-- artifact: https://claude.ai/code/artifact/e0d038a9-f414-4b93-bc0a-87391d3b78cf (docs/implementation-checklist.html) -->
# Remedy Scribe — Production Implementation Checklist

**Purpose:** everything between the current scaffold and a system that can legally and safely record real consultations in a Remedy clinic.
**Companion docs:** `remedy-scribe-prd.md` (what/why) · `remedy-scribe-roadmap.md` (when) · `docs/tech-stack.md` (with what)

---

## Refresh log — 2026-09-03 (Deployment enablement — the free-tier track)

**Progress: 117/122, unchanged — and that is the point.** No checklist item was added or ticked, because none of this is new product. It is the work of making an application that runs on a developer's laptop run on hardware nobody has to pay for, which Phase 5 specified (decision 0036: one VM, S3-compatible storage, managed Postgres) but which the actual deployment constraint — free tiers only, **Netlify** because the engineer owns that account, **Google Drive** because he asked for it — does not match.

Four commits of runnable-ness, then one of adaptation:

**The app was not actually runnable, and two independent bugs said so.** Every seeded account was unloginnable: no `mfa_secret` was ever written, and the seed domain `.invalid` is rejected by `email-validator`, so login returned 422 before it could even fail properly. Both now covered by regression tests that go through the **real login endpoint**, because both bugs lived in the gap between "the seed script succeeded" and "a human can log in."

**Path A walked in a real browser, and 6 of 23 checks failed.** There was **no list-encounters endpoint at all** — eight seeded notes existed and the UI could reach none of them — so `GET /encounters/recent` was added. And the grounding help text was gated on `audio_state === "available"`, meaning the text explaining that audio is unavailable **hid itself precisely when it was needed**; it is now gated on `spans_fit`.

**Google Drive is not S3-compatible, so it needed code** (decision 0040). `storage.py` became a dispatcher, the S3 implementation moved verbatim to `storage_s3.py`, Drive lives in `storage_drive.py`, and `STORAGE_BACKEND` selects between them with `s3` still the default — so **no call site changed**. Two facts decided the design and neither is in Google's prose documentation: a browser can PUT to a resumable session URI with no credentials (only demonstrated in sample code), and Google's upload host allows cross-origin PUT with `content-range` — undocumented, so it was tested over the wire *before any code was written*. Had that come back negative, audio would have proxied through the API in **both** directions.

**The adapter found a client bug that would have failed every multi-part Drive upload.** The uploader checked `response.ok`, and `308 Resume Incomplete` is not ok — it is Drive's success for every chunk but the last. Every long consultation would have died at the first chunk with "Part 1 upload failed (HTTP 308)", an error that reads like a server fault and is in fact success.

**Three costs are stated rather than absorbed**, because none is fixable in code: a free Google account **cannot use a service account**, so audio is owned by a named human out of their Gmail-shared 15 GB; Drive has **no presigned GET**, so playback is proxied through `GET /encounters/{id}/audio` (honouring `Range`, `no-store` set by hand) rather than returning a URL that would 401 in a browser — the dead play button decision 0030 exists to prevent; and Drive has **no lifecycle rules**, so decision 0033's storage-layer retention backstop is gone and only the Celery purge remains.

**Verified:** **450 API tests** (up from 419) with Postgres and MinIO up so nothing was skipped, 20 of them new; `ruff`, `mypy` and `tsc` clean; migration `c7e8f9a0b1d2` applied against real Postgres with the drift gate reporting no drift. **Not verified, and listed as such in the runbook:** everything needing real Google credentials — a live browser PUT, `Range` through the playback proxy, and a genuinely resumed upload.

📚 See `docs/runbooks/deploy-free-tier.md`, which now also says which checklist steps need the Drive adapter (recording, playback, retention) and which do not (everything else), so the engineer can stand the stack up and verify most of it before a single Google credential exists — and so a Drive problem stays distinguishable from a Netlify one.

---

## Refresh log — 2026-08-29 (Phase 6 — pilot instrumentation, and the last engineering phase)

**Progress: 117/122.** All six Phase 6 items. **419 API tests passing, up from 385**; 61 web unit tests; `ruff`, `mypy` and `tsc` clean; the migration gate reports no drift. The five items still open are Phase 7 P1 fast-follows, which are post-go/no-go by design. Three previously-open items were also closed as **obsolete** rather than left to inflate the count — Luna (decision 0021) and the Android and iOS background-audio items (decision 0024) describe work that will never happen.

### The 2.6 🧠 is answered, and the obvious answer was unsafe

A "minor edit" is **small *and* clinically inert** (decision 0039). Both halves are load-bearing, because a pure distance threshold — the tempting definition — produces this:

| edit | distance | what it actually is |
|---|---|---|
| `500mg` — `5000mg` | 1 character | a **10x overdose** |
| `500mg` — `500mcg` | 1 character | a **1000x error**, identical digits |
| `no chest pain` — `chest pain` | 3 characters | an **inverted finding** |

Each is *maximally minor* by distance and among the most consequential corrections a doctor can make. A metric scoring them as "the draft was basically right" would report the model as most trustworthy exactly where it was most dangerous — **in the very number being used to justify skipping vendor validation.** So the clinical check is a **veto, not a weighting**: no similarity score can make a changed dose minor. It covers quantities, dose units and negations, **including Filipino negations** (`wala`, `hindi`, `walang`), because P0-3 keeps Taglish verbatim and a negation flip in the patient's own words is the same inversion.

The error budget is deliberately asymmetric. A false positive makes the metric pessimistic — a number that under-sells the product. A false negative is a changed dose reported as trivial. Only one of those is recoverable after the fact.

### The heads-up has aged worse than it was written

It says the stated mitigation for skipping the vendor bake-off was "watch the edit-burden metric closely from day one of internal alpha." Since then, **decision 0035 swapped the note generator to a different vendor entirely** — also without a bake-off, also on the strength of watching this metric. **The mitigation is now carrying two unvalidated vendor choices**, and until this phase it was carrying them with nothing underneath.

### Three ways to get the arithmetic wrong, all avoided deliberately

**Summing revisions instead of comparing endpoints.** Edits save on blur (2.6), so typing a word, deleting it and retyping it makes three revisions and zero net change. The target asks how far the *signed* note is from the *drafted* one.

**Ordering revisions by timestamp.** Decision 0027 already recorded that identical `created_at` values make that non-deterministic. The generated text is instead found structurally: **the `previous_text` that never appears as any `new_text`**. Edits chain A—B—C and only A is nobody's output. Its one real ambiguity, an edit-then-revert, is *reported* rather than guessed at.

**Computing it later.** Phase 4.4's retention purge deletes revisions while the signed note is permanent, so a metric depending on live revisions would quietly become uncomputable mid-pilot. It is frozen at signing, keyed by definition version so a redefinition writes new rows beside the old ones instead of rewriting history — which is what makes "a metric redefined mid-pilot tells you nothing" a solved problem rather than a warning.

### Three existing safety nets caught the new code

**Phase 4.1's rotation-coverage test fired on the first new encrypted column since it was written.** Adding `EncounterRating.comment` made it fail — by design, since it asserts an *exact* set. The rotation script had already auto-discovered the column, but the test still forced a human to confirm the coverage was intended.

**A test passed for the wrong reason — the Phase 1.5 trap again.** `test_capture_never_blocks_a_signature` patched the function in its defining module, but `pilot_metrics` binds it at import with `from ... import`, so the patch never applied and the real function ran. Had the assertion been weaker it would have gone green while proving nothing about the safety property it exists for. Same shape as Phase 1.5's module-level dispatch dict defeating `monkeypatch`.

**A docstring asserted a safety property the code did not have.** `EncounterRating.comment` was documented as "encrypted at rest like any other free-text clinical field" while declared as plain `Text`. Caught before the migration was generated; there is now a test that plants a patient name and reads the raw column back.

### Metrics reported with their limits attached

⚠️ **"Correctly filed" is a caught-error rate, not a correctness rate.** The system sees filings it *rejected* (P0-6's 409s). It cannot see a note filed to the wrong patient that the doctor confirmed anyway — at that point every check agrees. Presenting it as the true rate would be the most flattering possible reading of the data. ⚠️ **The threshold (0.90) is calibrated against nothing** and is labelled as a guess; the first two weeks of real notes should move it **once**, before alpha, with the version bumped. ⚠️ **The metric is not length-neutral**, measured rather than assumed: the same two-word swap scores 0.84 in a nine-word Assessment and passes comfortably in a forty-word one, so a clinic whose notes run short will look like it edits more. ⚠️ **Unsafe-acceptance rate has no denominator yet** — the sampling workflow exists, but the reviewer's verdict is recorded nowhere, so the rate is computed off-system.

---

## Refresh log — 2026-08-29 (Phase 5 — deployment, observability and CI/CD)

**Progress: 108/122, up from 93.** All 15 Phase 5 items, built by three agents in parallel on non-overlapping file leases. **385 API tests passing, up from 330**; 61 web unit tests; `ruff`, `mypy` and `tsc` clean.

### The pattern held: the new checks found things nothing had ever looked at

**The migration gate ran red on first contact with the real tree** and found **four genuine divergences between the models and the deployed schema**. Two migrations created a `UniqueConstraint` *and* a unique index on the same column (`transcripts.encounter_id`, `refresh_tokens.token_hash`) — a second B-tree maintained on every insert for one guarantee, confirmed by reading the live table. Two enum columns were widened in Phase 0.4 without an `ALTER`. All four were harmless, which is exactly why they survived four phases: **nothing had ever compared the models against the deployed schema.** Closed by migration `b1c2d3e4f5a6`, verified reversible, baseline emptied — the gate now reports *"no drift: the migration chain fully expresses the models."*

That gate is a **snapshot assertion, not an ignore list**, and the difference is the point: it failed when the four divergences *disappeared*, forcing the baseline to be emptied rather than left as four standing holes a future drift could land in. An ignore list only ever grows.

**`apps/api/.env` was being baked into the Docker image.** `COPY . .` with no `.dockerignore`, proven by building the image and reading `/app/.env` out of it — containing a working `PHI_ENCRYPTION_KEY`. On this machine that key is *not* the published dev secret, so **Phase 4.1's boot guard would not have caught it**: the image would have started cleanly with a real key baked in.

**The per-IP login rate limit would have silently become per-clinic.** `auth.py` reads `request.client.host`, which behind a proxy is the proxy — so 10 attempts/minute becomes 10/minute for the entire clinic. Nothing flagged it because it only exists once there is a proxy. And the obvious fix is worse than the bug: uvicorn with `--forwarded-allow-ips=*` trusts the leftmost, client-supplied `X-Forwarded-For` entry, letting an attacker rotate their apparent IP per attempt.

### The 🧠, and the part that is explicitly not engineering's

**5.1 resolved to one Linux VM on Docker Compose**, with managed Postgres as the single bought service and Redis self-hosted. Kubernetes was rejected on the checklist's own principle: the compliance bar is about *where data lives and who can read it*, and a control plane moves neither. Postgres is bought because PITR is the genuinely hard part — continuous WAL archiving plus an *exercised* restore — and a lost clinic day of signed notes is a reportable DPA availability incident, not an outage. Redis stays self-hosted with the loss window quantified rather than hand-waved: it holds only the Celery broker (decision 0008 already put sessions and rate-limit counters in Postgres), so loss is about **one second of enqueues**, and Phase 1.5's stuck-encounter sweep already recovers that case within 35 minutes.

⚠️ **The jurisdiction and provider remain Legal's**, and decision 0036 puts it to them as four ordered questions rather than a vague ask: must PHI at rest stay physically in the Philippines; does that bind the *processor* boundary, given Groq already holds the audio and transcript; does key material follow the same rule; is the provider DPA signed before the first real recording.

### Liveness and readiness now mean opposite things, on purpose

They have opposite consequences — liveness failing **restarts the container**, readiness failing only **removes it from traffic** — so a liveness probe that checked the database would turn a brief Postgres blip into a crash loop. `/health` therefore takes no dependency at all; `/ready` checks Postgres and Redis. Driven red and back by stopping containers underneath it: Redis down gave 503 with `/health` still 200; Postgres down gave 503 with `/health` answering in 0.003 s; both back gave 200 **with no app restart**. Object storage is deliberately excluded, because gating on it converts a partial outage into a total one when consent, worklist, matching, review and signing all still work.

A measurement reframed the timeout budget: a stopped Postgres takes **4.1 s** to fail, not because of a timeout but because `localhost` resolves to both `::1` and `127.0.0.1` and libpq pays ~2 s per address. The budget is `connect_timeout` — which libpq floors at 2 s — multiplied by address count, so a managed endpoint with both A and AAAA records doubles it silently.

### PHI stays out of logs mechanically, not by convention

The heads-up's specific trap turned out to be real: **`include_local_variables` defaults to `True` in sentry-sdk**, and a stack frame inside `generate_note` holds the entire transcript while one inside `_build_section` holds the note. Nothing about the failing code has to be PHI-related — an unrelated `AttributeError` anywhere down that stack would have shipped the consultation to a third party. It is off at init, asserted against a fake SDK rather than trusted from the docs, and the initialisation **fails closed**: an SDK that rejects any safety kwarg gets no DSN at all.

Scrubbing is enforced at boundaries a careless call site cannot route around — a single process-wide log record factory, a formatter that *assembles* from an allow-list rather than filtering a denylist, and a Sentry `before_send`. The one honest gap is stated rather than implied: short unregistered free text is shape-indistinguishable from an ordinary error message, so a source scan bans f-strings in log calls outright.

### A cost measurement that reframes the PRD's target

Break-even against the PRD's **<$0.10/consult** is **51.7 minutes of audio** — 30 minutes costs $0.058, 60 minutes $0.116. ASR is **15—23x** note generation across the range, because Groq bills Whisper per audio-hour and `gpt-oss-120b` per token. **The cost target is therefore a *duration* budget**, and no amount of prompt tuning moves it.

### Stated plainly: what is written rather than running

⚠️ **Nothing is deployed.** No VM, domain, certificate, managed Postgres or bucket exists; no TLS handshake has ever happened; PITR is a spec, not a tested path. ⚠️ **No CI job has run on a GitHub runner** — `actionlint` was unavailable, so action *input names* were never schema-checked and the first push is the first real run. ⚠️ **Nobody has a Sentry account**, so today a `critical` alert is an ERROR line in a log file nothing reads — which means the "nothing watches anything" gap from Phase 4 is now *instrumented* but still not *delivered*. ⚠️ The health monitor runs inside the Beat process it watches, so it catches a wedged sweep but not a dead Beat. ⚠️ **Playwright stays out of CI on purpose** (a six-process browser suite is the flakiest thing in a pipeline, and a job people learn to re-run teaches that habit for the migration gate too) — the cost being that no full end-to-end path is automatically verified today.

---

## Refresh log — 2026-08-28 (Phase 4 — security and compliance hardening, plus the Groq vendor swap)

**Progress: 93/122, up from 75.** All 18 Phase 4 items are done, built by four agents working in parallel on non-overlapping file leases. **326 API tests passing, up from 206** — 45 new for audit logging, 33 for key rotation and security headers, 21 for retention, 19 for the Groq provider, 4 for credential upgrade. `ruff` and `mypy` clean; 61 web unit tests, `tsc` clean.

### Note generation moved to Groq — and the stated reason for it did not survive checking

Asked for because Groq has a free tier. **It cannot carry this workload.** The free tier allows roughly **8,000 tokens per minute**; a 20-40 minute consultation transcript is 10,000-20,000 tokens sent in one fused call, so *a single real consultation* exceeds the per-minute allowance. Worse, Groq's BAA covers "Covered Cloud Services", a definition that **explicitly excludes services "provided for free or at no additional charge"** — so the free tier is precisely the tier carrying no data-protection undertaking. The recommendation is not "don't"; it is **"do this, on the paid plan."**

**Because the change is right for a different reason.** Phase 1.3 already sends every consultation's *audio* to Groq (decision 0018), so Groq already holds the most sensitive artifact in the system. Generating the note there discloses nothing new, and collapses two AI processors into one: one DPA instead of two, one entry in the Data Privacy Act processor disclosure, one vendor in the breach runbook 4.3 was writing at the same time. Verified from their Services Agreement: **"Groq is not permitted to use Inputs or Outputs for training or fine-tuning"**, no retention by default, Zero Data Retention available.

The port was not a model-ID swap. Groq's docs are explicit that **structured outputs and tool use are mutually exclusive**, so forced tool use became `response_format: json_schema, strict: true` — which is *stronger*, since constrained decoding means the model cannot emit a schema violation. `qwen/qwen3.8-27b` would have given 10x the daily token budget and was rejected: it is Preview status, which the BAA also excludes. A model that cannot lawfully carry PHI is not a candidate regardless of quota.

### Three latent security defects, none of them in the new code

**A PHI leak into an unencrypted column.** `_extract_tool_input` interpolated the *entire* Anthropic response into its exception message, and `pipeline.py` writes `str(exc)[:500]` into `Encounter.last_pipeline_error` — a plain `String(500)` whose own comment argues it is safe because pipeline exceptions "can never leak PHI". On that path the claim was false: a response missing the tool block typically contains the model's prose *about the consultation*. Fixed on both providers; two tests plant a patient name and assert it cannot reach the message.

**`PATCH /notes/{id}` wrote no audit row at all** — the only change to clinical content invisible to the trail. A `NoteRevision` is not a substitute: it holds the PHI text, it dies with the note, and compliance cannot see it. `POST /patients/match` had also been unlogged since Phase 0.2, because it reads like a write and is in fact a read.

**`TRUNCATE` would have emptied the audit log** while leaving "append-only" technically true, because row triggers do not fire on TRUNCATE. The new audit table has a statement-level guard. ⚠️ **The consent ledger still has that gap** — it is P0-1's table, was deliberately left alone, and is filed as a follow-up.

### Two measurements that overturned an assumption, again

**Key rotation was rehearsed, not described.** At 17,500 rows / 35,000 encrypted values / 1.73 GB on real Postgres: **181.0 s**, then verified 35,000/35,000 readable under the new key alone and 0/35,000 under the old. Two surprises. **Cost is bytes, not rows** — transcripts are 14% of values and 91% of the time, so rotation cost is O(recorded minutes retained), which makes 4.4's deletion job a rotation-cost control. And **crypto is only 15% of it** (decrypt 16.5 s, +re-encrypt 26.7 s, +`UPDATE` 181.0 s), so neither `pgcrypto` nor a KMS would make rotation meaningfully faster. That is the second time on this codebase a measurement has moved blame off decryption; see 0029.

**The `bcrypt==4.0.1` pin was three versions early.** Tested rather than assumed: passlib 1.7.4 works fine with bcrypt 4.1.1 and 4.3.0 (merely logging an `AttributeError`) and only breaks hard at 5.0.0. The project had held a security-critical library backwards for years on the strength of a log line. Now argon2id, with bcrypt verify-only so nobody is locked out, and passlib — unmaintained since 2020 — removed entirely, which deletes the *reason* for the pin rather than carrying it forward.

### The 🧠s, and what was deliberately not decided here

**4.1 (PHI at rest)** resolved to keeping app-layer Fernet: `pgcrypto` puts the key into the SQL statement and therefore the query log, costs the test suite its database, and does not solve rotation either. **The final call is explicitly the DPO's**, along with HSM/KMS custody and whether deploy access should equal PHI access (today it does). **4.4 (retention mechanism)** resolved to *both* layers, as the checklist itself reasoned — bucket lifecycle as the storage backstop, a Beat job for the derived rows Postgres holds — while leaving untouched the part that is genuinely the user's: *how long*.

### Stated plainly: what was documented rather than done

⚠️ **Secrets management is a written design, not an implementation**, because there is no deployment yet (Phase 5). ⚠️ **The breach runbook has never been exercised, and its legal section needs counsel** — `privacy.gov.ph` returned HTTP 403 to direct fetching, so NPC Circular 16-03 was not read first-hand and the runbook says so rather than implying it was. ⚠️ **Remedy has no designated DPO and no breach response team**; both are legally required, and the roles table ships empty on purpose. ⚠️ **TLS is the application half only** — HSTS, CSP and secure headers ship; termination, the TLS 1.2 floor, and TLS to Postgres/Redis/object storage are Phase 5's, and the runbook names them rather than letting green headers imply coverage. ⚠️ **CI has never run on a GitHub runner** — every step was rehearsed locally and the YAML validated structurally, but the first push is the first real run.

---

## Refresh log — 2026-08-27 (Phase 3 — the grounding UI, the product's trust mechanism)

**Progress: 75/120, up from 71.** A doctor can now click any line of a drafted note, see the transcript passage it cites, and hear that moment of the consultation. This is what the last four phases were building toward — segment IDs assigned at persist time (1.2), the model citing those IDs instead of inventing offsets (1.4), citations verified rather than trusted (1.4). If any of them had cut a corner, it would show here.

**Phase 3 has no 🧠, but it has one rule that decided everything.** For a feature whose only job is proof, **a confidently wrong answer is worse than no answer.** An empty panel tells a doctor "check this yourself." A panel highlighting the *wrong* sentence tells them "this was verified" — and they sign on the strength of it. That failure is silent, and it is where every obvious implementation ends up. Three of them:

**Stored character offsets stop being true the moment a doctor edits.** `source_spans` holds offsets into the section text *as generated*, and P0-5 requires free editing before signing. An insertion in the first sentence shifts every later offset — and slicing by stale offsets still *works*, it just returns the wrong substring, confidently. So the offsets are re-validated against the note's current text rather than trusted: generation joins per-sentence strings with a single space, so slicing by the stored spans and re-joining must reproduce the text exactly. When it doesn't, grounding for that section is **withheld and explained**, not approximated. A *same-length* rewrite is the subtle case — the offsets stay structurally valid while the words become the doctor's — so "edited since drafting" is reported as a second, separate flag. Two questions, two answers.

**The database's belief about audio is not evidence.** The bucket's own lifecycle rule expires recordings after `audio_retention_days` and **nothing writes back to the encounter row**, so a set `audio_object_key` with a NULL `audio_deleted_at` does not mean the bytes exist. Trusting the row is exactly how a doctor gets the dead play button this phase's heads-up warns about. A `HEAD` runs before any play button is offered, and the row is corrected when the object is gone. Five states rather than two, because the *reason* is what a doctor needs: **withdrawn** (deleted at the patient's request — a legal event under P0-1, not the passage of time) reads differently from **expired**, and **unreachable** is deliberately not rounded up to "deleted".

**The transcript is not shipped wholesale to render a highlight.** Only cited passages plus one neighbour either side are returned. The transcript is the most sensitive artifact in the system — verbatim, including whatever the doctor chose *not* to write down — and a 30-minute consult is hundreds of segments. Neighbours come along flagged as context and ranked visually below the cited ones, because context helps but a neighbour is not evidence.

**Playback plays a window, not a file.** It stops at the cited passage's end rather than running on through the consultation — a doctor asked to hear one line's source, and continuing past it keeps disclosing the recording without them noticing.

**Three latent bugs surfaced, none of them in Phase 3's own code.** `--card2` has been referenced by the prior-visit card since 2.6 but never defined, so that card has had no background since it shipped — a CSS `var()` with no fallback fails silently and nothing in `tsc`, the build or any test sees it. And three smoke helpers still pinned IndexedDB to version 1, which throws `VersionError` now the store is at v2: **`consent-flow.cjs` and `record-flow.cjs` have been unrunnable since Phase 2.2**, failing with a driver error partway through rather than a check failure, which is why nobody noticed.

**A failing assertion that was right about the code and wrong about the clock.** The first end-to-end run reported "audio playback starts on the second tap: FAIL". Playback was working — the cited passage is 1,740 ms long, and by the time the assertion ran 2,500 ms later it had already stopped at `end_ms`, exactly as designed. Diagnosed by driving `new Audio()` against the presigned URL in the page and reading back `readyState`/`currentTime`. The fix made the test better: it now polls within the passage's own length, then asserts the far more valuable property — that playback *stops* and does not run on.

**Verified:** API **206 passing** (up from 173), `ruff`/`mypy` clean. Web **60 unit tests** (up from 42), `tsc` clean. End-to-end **39/39** against real Postgres, Redis, MinIO and real Chromium with a real 14-second recording — including HTTP **206** on a Range request, no audio on the first tap, playback stopping at the passage end, and the withdrawal path degrading to transcript-only with the reason in words. Regressions all green: note-flow 32/32, consent-flow 35/35, record-flow 22/22, auth-flow 17/17.

⚠️ **Stated plainly: `GROQ_API_KEY` and `ANTHROPIC_API_KEY` are not provisioned in this environment**, so the ASR and note-generation legs could not run. Rather than skip end-to-end verification, `smoke/seed_pipeline.py` substitutes those two vendor calls behind an explicit `SEED_PIPELINE=1` flag — the recording, the upload, the object in MinIO, the presigned playback, the Range requests, the span resolution and the whole browser UI are all real; only *how the draft came to exist* is stood in for, and that path is verified for real in 1.3 and 1.4. Consequently `upload-queue.cjs` reports 3 of 18 failing, all of them "pipeline_status is still `uploaded`" — which is the **correct** behaviour when transcription cannot run, and P0-2's rule that local audio survives until the pipeline confirms working as intended.

---

## Refresh log — 2026-08-27 (Phase 2.5 + 2.6 — Phase 2 complete, the journey runs end to end)

**Progress: 71/120, up from 61.** The whole clinical journey now runs as one flow: consent → record → upload → transcribe → generate → link a patient → review → edit → file → sign. Every earlier phase built one link; this is the first time the chain is complete and tested as a chain — **30/30 end to end** against Postgres, Redis, MinIO, a Celery worker and a real browser.

**The 2.5 🧠 was resolved against the requirement, and the checklist's own preferred option turned out not to fit.** It proposes a blind index for searching encrypted names; an HMAC supports *equality only*, while P0-6 requires a typed or dictated name to **fuzzy-match**. "Maria Cruz" for *Maria Santos Dela Cruz*, or "Cruzz" for "Cruz", would return nothing. So names are decrypted and ranked in Python — **and that was measured, not assumed**: the naive version took **2.1 seconds at 5,000 patients**. The breakdown redirected the fix entirely (raw `SELECT` 7.7 ms, decrypt 118 ms, but full **ORM** 348 ms and unfiltered `difflib` 183 ms): decryption is not the bottleneck, ORM hydration and unfiltered similarity are. A raw three-column `SELECT` plus a token prefilter brings it to ~194 ms, roughly 10× better. Decision 0029 records the scale ceiling as numbers and names the correct next step — a *token-level* blind index, which preserves fuzzy matching instead of replacing it with equality.

**Identity is now genuinely re-confirmed at filing, not nominally.** Filing requires the caller to *name* the patient, and the server checks it against the encounter's own `patient_id`. Reading that field and trusting it would make the check a formality; a stale client showing the previous patient is rejected with 409 rather than silently corrected. Filing is the last cheap moment to catch a mis-linked recording — after it, the note is in the wrong person's history.

**A feature can be complete and still be dead code.** Writing 2.6's end-to-end test surfaced that the review screen was **unreachable**: notes are 1:1 with encounters but nothing exposed the note id, so `/notes/{id}` had no route into it from any worklist. The test could not navigate to the screen it existed to exercise. Fixed by adding `note_id` to `EncounterOut`. Unit tests and a typecheck both pass happily on a screen no user can reach.

**The 2.6 🧠 — what counts as a "minor edit" — is deliberately still open**, deferred to Phase 6 rather than guessed. 2.6 does not need it, and picking one here would bake a measurement decision into a UI phase. What 2.6 guarantees is that the choice stays free: `NoteRevision` stores full before/after text for *every* edit, so any candidate definition can be computed retrospectively over the same data.

**Verified:** API **173 passing** (up from 146), `ruff`/`mypy` clean. Web 42 unit tests, `tsc` clean, build fine. The end-to-end run asserts the things that would be expensive to get wrong: recording starts with **no** patient linked (P0-6's "never blocked on identity"), sections render **APSO** rather than SOAP, signing straight from `generated` is rejected, filing against the **wrong** patient is rejected rather than silently corrected, signing without a PRC licence is rejected, and a signed note is immutable.

---

## Refresh log — 2026-08-27 (Phase 2.4: offline upload queue — the loop closes)

**Progress: 61/120, up from 55.** Audio recorded on a laptop now reaches S3, the Phase 1 pipeline runs on it for real, and the local copy is deleted only once that has happened. Until this phase, everything built server-side in Phase 1 had no real input.

**The write-ahead invariant, asserted rather than described.** The queue entry — carrying the idempotency key — is written when recording *starts*. The end-to-end test checks exactly that: at t+0.9s the entry exists in state `recording` with its key persisted and **zero chunks on disk**. That ordering is the point of the phase: a key generated in memory and lost to a crash produces a second key on retry, which is the duplicate-encounter bug the key exists to prevent.

**"Receipt" and "pipeline start" are different events, and only one of them is a 200.** `upload/complete` confirms S3 holds the object and a Celery chain was *enqueued* — nothing about whether a worker ran. A broker outage leaves `pipeline_status` at `uploaded` forever, so deleting on the 200 would destroy the only copy of a consultation whose processing never began. The queue therefore has a distinct `uploaded → confirmed` step polling a new `GET /encounters/{id}`, advancing only at `transcribed`. The mirror case matters too: a *terminal* server failure keeps the local audio, because it may be the only copy.

**Offline is not a failure.** `OfflineError` neither increments the attempt counter nor escalates the backoff — counting an outage toward the retry ceiling would dead-letter healthy recordings during exactly the event this queue exists to survive. Backoff is jittered so several laptops recovering from one wifi outage do not hit the API in synchronised waves.

**Two bugs found by reading the end-to-end test's own output, not by its assertions.** Both left the upload working while making the status readout lie — and the status readout *is* the P0-2 requirement. (1) A normally-stopped 14-second recording was labelled *"Recording was interrupted"* and queued for upload **while still capturing**, because the recovery pass could not tell a crashed recording from a live one; fixed with a heartbeat plus a staleness window. (2) The panel showed *"56 KB of 37 KB"* — progress over 100% — because the byte total came from React state captured before `stop()` flushed the final chunk; now derived from the chunk store. Both have regression assertions.

**Verified against the real stack**, not mocks: Postgres, Redis, MinIO, a Celery worker, and a real Chromium. **18/18** end-to-end including a genuine 48,774-byte presigned upload to MinIO, `pipeline_status=note_generated`, and local chunks going 3 → 0 only after that. API **146 passing** (up from 143, including a route-order regression guard — a path parameter declared before `/loose` would silently swallow the worklist). Web **42 unit tests** (up from 18), `tsc` clean, build fine.

---

## Refresh log — 2026-08-27 (Phase 2.3: consent flow — the legal gate)

**Progress: 55/120, up from 49.** The consent flow is complete as a mechanism: bilingual script, participant roster, decline, mid-visit re-consent pause, and withdrawal that actually deletes. **It cannot ship to patients yet, and not for an engineering reason** — the script text is a placeholder written by an engineer, and RA 4200 clearance by Philippine counsel is the PRD's own *blocking* open question. The app displays that caveat on screen, and the text is isolated in one file so counsel's version is a single edit.

**P0-1's first two bullets constrain each other, and reading either alone gets it wrong.** Bullet 1: the script is presented "before anything is captured". Bullet 2: once consent is given, "the spoken exchange is captured as the first segment". The tempting reading — start recording, read the script, and the recorded asking becomes segment 1 — satisfies bullet 2 and **violates bullet 1**. The only sequence satisfying both is roster → script → log the outcome → start recording → speak a short confirmation. So the consent screen never touches the microphone, and a smoke check asserts it. The consequence Legal needs told explicitly: the patient's own spoken "yes" is *not* on the recording, only the doctor's confirmation that it was given.

**Decision 0003 is closed after being open since Phase 0.1 — by elimination, not by choice.** It offered manual flagging vs. ASR-diarization detection of a new speaker; decision 0018 removed diarization entirely, so there are no speaker labels for the second option to read. Manual flagging is the only implementable option, and the original concern (a doctor mid-exam simply forgetting) is unaddressed — what the design does instead is make remembering cheap and the state honest: the pause happens before any network call, and resuming is gated on the ledger write rather than the doctor's word.

**Withdrawal now has real server-side effects**, closing the checklist's own heads-up. Ledger entry committed first (the legal record), retention clock set to now (durable backstop), then a best-effort immediate object delete. No attempt is made to kill a running Celery task, and the UI says "next stage boundary, not instantly" — asserted by a smoke check, because that sentence is also what Legal will be told.

**Pausing turned out to collide with 2.2's gap detection three ways**, each producing a confidently wrong reading: the worklet counting samples the recorder no longer writes (pause reported as lost audio), the stall detector firing on expected silence, and the pause duration reading as a system suspend. Plus one that silently eats audio: a paused `MediaRecorder` ignores `requestData()`, so `stop()` must resume before flushing or the tail is discarded.

**Verified:** API **143 passing** (up from 136), `ruff`/`mypy` clean. Web 18 unit tests, `tsc` clean. And **35/35 end-to-end in a real Chromium** against the live API, with every check mapped to a P0-1 clause — including that the microphone is never touched on the consent screen, that paused time is reported separately rather than as missing audio, and that withdrawal takes local chunks from 3 to 0.

---

## Refresh log — 2026-08-27 (Phase 2.2: recording)

**Progress: 49/120, up from 43.** The app records for real: mono Opus at 32 kbps, AES-GCM encrypted before anything touches disk, written in ~5s chunks to IndexedDB so a crash or a lid-close costs at most one chunk. Everything load-bearing came from the capture harness rather than assumption — the wake lock re-acquired on every return to visible, an explicit `audioBitsPerSecond`, and a requested constraint never trusted as an achieved setting.

**The P0-1 consent gate is real and it is the only path to the record button.** P0-1 requires blocking *"before anything is captured"*, and the existing server enforcement (upload confirmation, transcription) both run after capture — so this needed a new read, `GET /api/v1/consent/{encounter_id}`, built on the same ledger fold `assert_consent_valid` uses. It fails closed on every uncertain path including offline, which is a real UX cost stated plainly in decision 0026: assuming consent because we cannot check it would be unlawful recording under RA 4200. Until 2.3's bilingual flow exists a doctor genuinely cannot record — the correct state for a system whose legal basis is not yet implemented, rather than a temporarily-open path with a TODO.

**Audio gaps are now recorded rather than hidden.** Decision 0024 established that lid close loses audio and no client architecture can prevent it. Given that, the only honest design is to detect it: an AudioWorklet counts samples as ground truth, wall-clock jumps read as suspends, worklet silence as stalls, and missing time appears **in the recording indicator itself** with a plain-language cause.

**Verified:** API **136 passing** (up from 131), `ruff`/`mypy` clean. Web **18 unit tests** (vitest + a real `fake-indexeddb`, not a stub) covering what fails silently — key non-extractability, plaintext never reaching disk, cross-session leakage, IV freshness, GCM rejecting tampered chunks, reassembly order. And **22/22 end-to-end in a real Chromium** against the live API: gate blocks with no consent, recording runs once consent exists, 4 chunks / 65 KB land in IndexedDB with the WebM magic bytes provably absent, 18s elapsed against 17s captured and **0:00 missing**, withdrawal re-blocks.

**Decision 0025 confirmed in practice:** 65,218 bytes for 18 seconds is **~29 kbps**, against the 32 kbps target and the harness's accidental 129 kbps.

**One real bug found by a test hook timing out:** `getAudioKey()` leaked an IndexedDB connection. The production consequence is worse than the test symptom — leaked connections accumulate one per recording across a clinic day, and an open connection blocks `onupgradeneeded`, so a future `DB_VERSION` bump would hang for anyone with the tab open.

---

## Refresh log — 2026-08-27 (Phase 2.1: web app foundation, client re-platformed)

**Progress: 43/120, up from 37.** The client is now a **browser web app on a clinic laptop**, not an Expo mobile app (decision 0024). This was not a preference: the supervisor answered the PRD's open question *"what devices do doctors actually carry"* — laptops — and both reasons `tech-stack.md` §1 gave for rejecting a web client were specific to a phone. `apps/mobile/` is deleted (git history retains it); `apps/web/` replaces it.

**Measured before committing to it.** `docs/experiments/audio-capture-harness.html` ran 29 minutes on the real hardware: audio lost during 131s of backgrounding across 9 windows was **0.05s**, page timers did not throttle at all, and every measurable loss (6.5s of 7.7s) came from one system suspend. Lid close is the single real gap and it favours no architecture — it is OS power policy that neither a browser nor Electron can veto. Full accounting in decision 0024.

**Two backend changes a browser needs that a native client never did, both of which fail *silently*:** CORS (without it the preflight is rejected and the request never reaches a route, so nothing appears in the API log at all) and an **httpOnly refresh cookie**. The cookie is the one place the browser is strictly *stronger* than the retired mobile plan: JavaScript cannot read it, which `expo-secure-store` could never promise. Decision 0006 is amended, not reversed — the access token stays short-lived and in memory.

**Re-verified after implementing:** full API suite — **131 passing** (up from 121; 10 new in `tests/test_web_client_support.py`), `ruff` and `mypy` clean, postgres and MinIO testcontainer tests included. Web app: `tsc` clean, production build succeeds (79 KB gzipped, service worker precaching 8 entries). And a **17-check end-to-end smoke test through a real browser against the live API** (`apps/web/smoke/auth-flow.cjs`) — login with a live TOTP code, cookie asserted httpOnly and path-scoped, no token in any JS-readable storage, session restored from the cookie alone after a full reload, logout leaving no zombie session.

**Three real bugs found by running it, not by reading:**
1. **Refresh-token precedence was backwards.** Preferring the cookie over an explicitly-presented body token silently broke Phase 0.3's reuse detection — a caller naming a deliberately-stale token got the valid cookie rotated instead and received 200 where 401 was required. Worse, single-session logout would have revoked whichever session the cookie happened to hold rather than the one named. Body-first now, asserted directly.
2. **Cookie-clearing on the error path never reached the browser.** Mutating the injected `response` and then raising `HTTPException` discards the mutation — FastAPI builds a fresh response for the exception. A dead cookie left in place guarantees every future silent renewal fails identically instead of falling through to a real login. Now carried on the exception itself.
3. **A 422 rendered as "Could not sign in. Please try again."** — indistinguishable from wrong credentials, sending a doctor to re-check their password when the real problem is the email. Reachable by a real user, not just a buggy client: `a@b.test` passes the browser's own `type="email"` validation but the API rejects RFC 2606 reserved TLDs via pydantic `EmailStr`.

**One item deliberately deferred rather than silently dropped:** biometric unlock. The original item assumed a personal phone. On a *shared* clinic laptop, WebAuthn (Windows Hello / Touch ID) authenticates the machine's logged-in user, so if doctors share one Windows session a biometric prompt proves nothing about which doctor is signing — arguably worse than nothing, because it looks like proof. Needs an answer to "do doctors share a Windows login?" first. See the 🧠 in 2.1.

---

## Refresh log — 2026-08-25 (Phase 1.5: pipeline failure handling — Phase 1 now fully closed)

**Progress: 37/120, up from 33.** Two new terminal `EncounterPipelineStatus` members (`transcription_failed`, `generation_failed` — deliberately not a third `upload_failed`, decision 0023) back a real dead-letter path: `_mark_stage_failure` in `app/tasks/pipeline.py` records `retry_count`/`last_pipeline_error` on every failed attempt and flips to the terminal status once retries are exhausted, reset to a clean slate on the next success. `GET /encounters/failed` surfaces the dead letter (no app exists yet to surface it in); `POST /encounters/{id}/retry` re-runs only the stage that failed, not the whole pipeline — a `GENERATION_FAILED` retry never re-pays for a real ASR call the first attempt already got right.

**The 📚 "stuck work is the real hard part" note is followed as two separate mechanisms, not one (decision 0023):** dead-lettering only catches a task that ran and raised. `sweep_stuck_encounters`, on a new Celery Beat schedule (every 5 minutes, `infra/docker-compose.yml`'s new `beat` service), catches the other failure mode — a task that never ran at all — by comparing a new `pipeline_updated_at` column against a configurable staleness threshold instead of catching an exception that was never thrown.

**Re-verified after implementing:** full suite — **121 passing** (up from 107; 13 new tests in `tests/test_pipeline_failure_handling.py`, 1 new RBAC regression). `ruff` and `mypy` both clean (55 source files). Migration `c9d0e1f2a3b4` applied cleanly against a real Postgres container as part of the full run.

**One real bug found while writing the tests, not by reading:** the first version of `sweep_stuck_encounters` dispatched via a dict built at module load time (`{UPLOADED: run_pipeline, TRANSCRIBED: run_note_generation}`). That dict captures the two functions' identities once, at import — a test's `monkeypatch.setattr("app.tasks.pipeline.run_pipeline", ...)`, which replaces the module attribute, has no effect on a reference already stored in the dict. The test's fake never ran; the real `run_pipeline` did, which tried to open a real Redis connection and hung the test run. Fixed by dispatching on a plain `if`/`else` referencing the bare names inside the function body, so they resolve from the module's current global namespace at call time — see docs/progress/1.5-pipeline-failure-handling.md for the full account.

**Phase 1 is now fully closed** (1.1 through 1.5).

---

## Refresh log — 2026-08-25 (Phase 1.4: real note generation)

**Progress: 33/120, up from 28.** `HaikuNoteGenerator.generate` is implemented for real — a single fused Anthropic Messages call, forced tool-use for structured output (`tool_choice` pins the model to exactly one tool; no free-text parsing), APSO section order, hedged language required by the system prompt, and two mechanical (not instruction-following) suppression layers: low-confidence words become a literal `[INAUDIBLE]` in the prompt before the model ever sees them, and the schema's `suppressed` field forces empty text server-side regardless of what the model also emitted.

**The 🧠 "how do you get trustworthy source spans?" call is resolved (decision 0022):** segment IDs — reusing the transcript segment `id` already assigned at persistence time (decision 0016), not a new sentence-numbering scheme. The model cites `segment_ids`; any ID that doesn't match a real segment sent in the prompt is dropped, not trusted. Character offsets (`text_start`/`text_end`) are never asked of the model — the server computes them exactly by tracking a cursor while concatenating the model's own sentences.

**Re-verified after implementing:** full suite — **107 passing** (up from 91; 16 new tests in `tests/test_note_generation_haiku.py` covering prompt formatting, tool schema shape, span computation, citation-hallucination dropping, suppression enforcement, the API-key gate, the empty-transcript cost short-circuit, HTTP-error propagation, and a real (un-mocked) httpx request-building check plus a golden-transcript end-to-end case). `ruff` and `mypy` both clean (56 source files). Migration `b8c9d0e1f2a3` (`notes.prompt_version`) applied cleanly on top of the existing 7-migration chain.

**One test-suite fix required by this phase's own change, not a new bug:** `TranscriptSegment` gained an `id` field (Phase 1.4 needs a stable citation target), so three pre-existing round-trip tests in `test_transcript_persistence.py` that compared loaded segments against the `id=None` fixtures needed a `_with_ids()` helper — persistence assigning real IDs is the correct new behavior, the tests were asserting the old one.

---

## Refresh log — 2026-08-25 (note generation: Haiku only, Luna dropped)

**Progress: 28/120, unchanged.** A planning-ahead update to Phase 1.4, not new work done — the user's call (decision 0021): Claude Haiku 4.5 is now the sole note generator; `LunaNoteGenerator` and `app/services/note_generation/luna.py` are deleted, not kept dormant. **This drops the risk mitigation P0-4 named explicitly** — "Haiku remains available as a configured fallback if Luna underperforms" — since there is no longer a second real provider to fall back to. Not a defect; a deliberate trade the checklist item below is annotated with, same treatment as the ASR vendor swap (decision 0018).

**Re-verified after making the change:** full suite — **91 passing** (unchanged from before this edit — Phase 1.4 isn't built yet, so nothing exercised the deleted code path). `ruff` and `mypy` both clean (55 source files now, down from 56 — one file fewer). App boots and `/health` responds.

**A second finding, smaller but real:** the local (uncommitted) `apps/api/.env` still had `NOTE_GENERATOR_PROVIDER=luna` and a stale `ELEVENLABS_API_KEY=` line from before Phase 1.3 — invisible until now because `SettingsConfigDict(extra="ignore")` silently drops unrecognized keys, and `luna` only started failing once the `Literal` type narrowed to `["haiku"]` alone. `.env` drift under `extra="ignore"` is invisible by construction for any field not validated against a closed set — see decision 0021.

---

## Refresh log — 2026-08-25 (mypy baseline, ASR vendor references)

**Progress: 28/120, unchanged from the last run** (this refresh re-verified ground truth and audited existing code; it didn't advance any new checklist item). Full audit trail per subphase lives in `docs/progress/` and `docs/decisions/`.

**Re-verified this run, all fresh (not carried over from memory):** full test suite — **91 passing**, up from the 90 last reported, because this run's audit added one regression test (see below). `ruff check` — clean. `mypy` — **clean, for the first time this project has run it** (56 source files; previously never run — Phase 5.3's "type-check (mypy)" item was still unchecked and there was no config at all). Alembic's 7-migration chain resolves to a single head and applies cleanly end-to-end against a real Postgres container (exercised by `tests/test_postgres_specific.py`, not just read). App boots and `/health` returns `200 {"status":"ok",...}` against a live process.

**Two real, previously-undiscovered bugs found and fixed by this audit, not by reading:**
1. **`GroqWhisperProvider.transcribe` would have crashed on its first real call.** It passed `data` to `httpx.post` as a list of `(key, value)` tuples alongside `files=`; httpx's multipart encoder requires `data` to be a mapping when `files` is also present, and raises `TypeError` immediately. Invisible to the existing test suite because that test mocked `httpx.post` entirely, bypassing httpx's real request-encoding logic — exactly the class of gap a mock hides (gap-audit class 5, "guarantees your tests never construct," generalized past the DB layer). Fixed (`data={"timestamp_granularities[]": [...], ...}`, the dict-with-list-value form httpx actually expects for repeated multipart fields) and given a dedicated regression test that builds a real httpx request (no network) instead of mocking the call away.
2. **`ASRProvider.model_version` couldn't be a `@property` on a subclass** — mypy caught the LSP violation (overriding a writable base-class attribute with a read-only property) on its first run. Fixed by setting it as a plain instance attribute in `GroqWhisperProvider.__init__` instead.

**One confirmed environment-class limitation, not a bug in this codebase:** MinIO (`RELEASE.2022-12-02`, the version pinned in `infra/docker-compose.yml` and used by the test containers) accepts `PutBucketEncryption` and the `AbortIncompleteMultipartUpload` lifecycle action without error, but doesn't actually enforce either — confirmed by inspecting raw API responses directly, not assumed. Both are correct, standard S3 API calls that a real AWS bucket (this system's actual deploy target) accepts and enforces; see decision 0014.

**One requirement-coverage gap surfaced by the reverse-traceability pass (Step 2):** `remedy-scribe-prd.md`'s P0-3 explicitly requires "speaker diarization enabled." Phase 1.3 implemented ASR with Groq-hosted Whisper instead of the PRD's named ElevenLabs Scribe v2 (the user's explicit call) — Whisper has no diarization mechanism at all, so this half of P0-3 currently has no code behind it anywhere in the system, and won't until one of decision 0018's three options is picked. Not a defect in what was built; a real, currently-open gap against a written P0 requirement, flagged here so it isn't lost between now and Phase 1.4.

**Two small, real gap-audit findings, both cheap, both fixed:** `app/models/patient.py` and `app/services/note_generation/haiku.py` each had one unused import (ruff `F401`) — pre-existing, unrelated to any phase's active work, fixed in passing. `mypy.ini` added (didn't exist before) so botocore/celery's missing type stubs don't drown out real findings on the next run; `types-python-jose`/`types-passlib` added to `requirements-dev.txt` to resolve two more for real instead of suppressing them.

---

## How to read this

| Marker | Meaning |
|---|---|
`- [ ]` | A task. Check it off.
🧠 **Your call** | A real fork in the road. I've listed the options and what each costs you. Don't let me (or anyone) pick this for you silently — write your choice and your reason into `docs/decisions/`.
⚠️ **Heads-up** | A trap that is not obvious from reading the code. Most of these cost people days.
📚 **Understand first** | A concept to hold in your head *before* writing the code, or the code will look arbitrary.

**A note on how to use this while learning:** resist doing these top-to-bottom as dictation. For each 🧠, try to predict the tradeoff before reading my summary — then check yourself. The gap between your guess and the answer is the actual learning. For each ⚠️, ask "how would I have found this myself?" — usually the answer is a test, a type, or a log line you didn't have.

---

## Current state (verified, not assumed)

What actually runs today, confirmed by executing it this session — not by reading the README, and not carried over from an earlier run without re-checking:

**Real and tested (419 API tests + 61 web unit tests + 145 end-to-end browser checks; `ruff`, `mypy` and `tsc` all clean):** the data model (clinicians, patients, encounters, consent ledger, notes, revisions, transcripts, refresh tokens, login attempts, audit log); the full 9-migration Alembic chain, applied for real against a live Postgres container, not just read; the consent ledger's append-only Postgres trigger AND all three `CHECK` constraints (`Note.status`, `Encounter.pipeline_status`, `ConsentLedgerEntry.event`) — all exercised by tests that run real SQL against real Postgres, not asserted from the ORM layer. Consent *enforcement* (server-side, at both `upload/complete` and the head of `transcribe_encounter`). RBAC enforcement (`require_role` attached to every clinical-write route). Refresh-token rotation with reuse detection, login rate limiting/lockout, two-step MFA enrollment. The full presigned-multipart upload flow (`init`/`parts`/`complete`), idempotent end to end, tested against real MinIO via testcontainers — not mocked — including a real presigned-URL PUT round trip. Encrypted transcript persistence, wired into both ends of the Celery chain, each segment carrying a stable citation ID assigned at persist time (Phase 1.4). Real ASR integration (Groq-hosted Whisper large-v3, replacing the PRD's named ElevenLabs Scribe v2 — see the refresh log above and decision 0018) with a real (though never-run-against-a-live-key) HTTP call, turn-order-preserving parsing, and a regression test that builds a real httpx request rather than mocking the call away. Real note generation (`HaikuNoteGenerator`, Phase 1.4) — a single fused, structured-output call producing APSO sections with mechanically-enforced suppression and segment-ID citations verified (not trusted) before persistence; also never run against a live key, but exercised by a golden-transcript test and a real-request-building test the same way the ASR integration is. Pipeline failure handling (Phase 1.5) — dead-lettering into two real terminal statuses with a doctor-triggered `/retry`, plus a separate Celery Beat sweep for encounters that got stuck without ever raising an exception. **A browser client foundation (Phase 2.1)** — `apps/web/`, Vite + React + React Router, its API client generated from the live OpenAPI schema rather than hand-written, and an auth flow driven end-to-end through a real browser against the live API by a 17-check smoke test: login with a live TOTP code, an httpOnly path-scoped refresh cookie, no token in any JS-readable storage, and session resume from the cookie alone after a full page reload. **Real audio recording (Phase 2.2)** — gated behind a fail-closed P0-1 consent read, mono Opus at 32 kbps encrypted with a non-extractable key before it touches disk, chunked to IndexedDB, with audio gaps detected and surfaced rather than hidden; proven in a real browser including an assertion that plaintext audio never reaches storage. **The full consent flow (Phase 2.3)** — bilingual script, participant roster, decline, mid-visit re-consent pause, and withdrawal that deletes local audio and the uploaded object; 35 end-to-end checks, each mapped to a P0-1 clause. Its *mechanism* is complete; its script text still awaits counsel. **A durable offline upload queue (Phase 2.4)** — write-ahead entries in IndexedDB, presigned multipart upload to real object storage, jittered exponential backoff that does not punish being offline, a device-full guard measured in minutes of recording, and local audio deleted only once the server's pipeline has actually run. This is the phase that closes the loop: everything Phase 1 built server-side now receives real input from a real laptop. **Patient identity and the review/sign workflow (Phases 2.5 and 2.6)** — name-first fuzzy search over encrypted names (measured, then optimised ~10x), loose-session linking, identity re-confirmed at filing with a mismatch rejected rather than corrected, an APSO review screen with per-edit revisions, a deliberate PRC-licence signing ceremony after which the note is immutable, and prior-visit context drawn only from signed notes. **Phase 2 is complete, and the whole journey — consent through signature — is tested as one flow.** **The grounding UI (Phase 3)** — click a note line to see the transcript passage it cites, click again to hear that moment; offsets re-validated against the current text rather than trusted, audio availability verified against object storage rather than read from the database, only cited passages returned, and playback bounded to the cited window and signed `no-store` so nothing lands on the laptop's disk. **Security and compliance hardening (Phase 4)** — PHI-key rotation rehearsed for real (181 s over 1.73 GB) with a production boot guard that refuses the published dev key by fingerprint; audit coverage from 7 to 23 call sites under one rule, append-only with a TRUNCATE guard, and an access-report query that answers "who looked at this record?"; a retention job that finally reads two columns nothing read, deleting audio, transcripts and revisions while never touching a signed note; CI with dependency scanning that took 30 advisories to 1; argon2 replacing passlib and its backwards bcrypt pin; and a Postgres restore that was genuinely executed rather than documented. **Note generation now runs on Groq**, consolidating onto the vendor that already holds the audio. **Deployment, observability and CI/CD (Phase 5)** — a specified single-VM topology with managed Postgres, liveness and readiness split so a database blip cannot restart a healthy container (driven red and back against live containers), correlation IDs across the Celery boundary, PHI kept out of logs and error reports mechanically rather than by convention, a migration gate that **found four real schema divergences on its first run**, and staging seeded over the real upload path with six independent locks against pointing it at production. **Pilot instrumentation (Phase 6)** — everything the PRD promised to measure, in place *before* alpha rather than after: edit burden under a definition where a clinical-safety veto overrides similarity (so a one-character dose change can never be "minor"), documentation time split into total and review, voluntary use counted in distinct weeks rather than volume, a five-star prompt that never blocks, and a deterministic flag-first weekly review sample that returns note ids only. A live server driven end-to-end with curl through login → patient match → encounter → consent; `/health` returns 200 from a freshly booted process this session.

**Wired but hollow:** `Transcript.retention_expires_at` and `Encounter.audio_retention_expires_at` are both written on every relevant row and read by nothing (Phase 4.4 owns turning that into a policy — see its updated wording below).

**Absent entirely:** **any running deployment** — Phase 5 specified and rehearsed the topology and the free-tier track (2026-09-03) wrote the runbook and the Drive backend for it, but no Netlify site, Render service, Neon database or Google credential exists yet, and no CI job has executed on a real runner; **alert delivery**, since the rules exist but nobody has a Sentry account, so the Phase 4 "nothing watches anything" gap is now instrumented rather than closed; a patient **merge** tool; a designated **DPO and breach response team**, both legally required and currently nonexistent; and — a genuine, currently-open gap against a written requirement, not an oversight — speaker diarization (P0-3), which the ASR vendor in use structurally cannot provide.

The honest headline: **the engineering is done. Every remaining blocker is a signature, an account or a server.** A doctor captures consent, records, gets a draft note, clicks any line to see and hear where it came from, edits, files against a re-confirmed identity, and signs — and the system now measures how much they had to change, how long it took, and whether they came back next week.

**Every hardening phase paid for itself in defects nobody was looking for**, and the pattern held to the end. Phase 4 found a PHI leak into an unencrypted column and a `TRUNCATE` that would have emptied the audit log. Phase 5 found `apps/api/.env` being baked into the Docker image with a live key — which Phase 4.1's boot guard would *not* have caught — and four schema divergences nothing had ever compared. Phase 6 was caught **by its own predecessors**: 4.1's rotation-coverage test fired on the first new encrypted column since it was written, and a new test passed for the wrong reason in exactly the way Phase 1.5 did. None of these were in new code.

The last engineering decision was the one deferred longest, and the obvious answer to it was unsafe. A "minor edit" is now **small *and* clinically inert**, because `500mg` → `5000mg` is a one-character edit and a tenfold overdose: under a plain distance threshold the metric would have reported the model as most trustworthy precisely where it was most dangerous — in the number being used to justify skipping vendor validation for *two* vendors now, not one.

What remains is not engineering:

1. **Legal and the DPO.** Clear the RA 4200 consent script. Answer decision 0036's four data-residency questions. Confirm the breach runbook's obligations, assembled from secondary sources because the NPC site refused direct fetching. And **designate a DPO and a breach response team — both legally required, both currently nonexistent.**
2. **Accounts, and someone to press deploy.** For a **real pilot**: a paid Groq plan (the free tier cannot carry a consultation and is excluded from the BAA), a Sentry account (without it every alert rule from 5.2 is an ERROR line nobody reads), a managed Postgres, and a VM. For the **free-tier demo** the engineer asked for, that shrinks to five signups and a runbook — Netlify, Render, Neon, Upstash and a Google account — with the code for all five now written and tested. Either way **nothing is deployed**, and no CI job has run on a real runner. See `docs/runbooks/deploy-free-tier.md`, which is explicit about what the free tier costs you: no resident worker, no lifecycle-rule retention backstop, and audio owned by a named human rather than by the application.
3. **Two weeks of real notes** to calibrate the edit-burden threshold, which is currently a labelled guess — moved once, before alpha, with the version bumped.

Two numbers to carry into the pilot design. The PRD's **<$0.10/consult** target breaks even at **51.7 minutes of audio**, because ASR costs 15–23× note generation — it is really a *duration* budget. And the **≥70% minor-edit** target is now measurable, but not yet meaningful: nobody has seen the distribution it will be judged against.

Phase 0 through Phase 6 are closed. The five open items are Phase 7 P1 fast-follows, which are post-go/no-go by design.

---

## Phase 0 — Close the holes in what already exists

Do this first. These are not new features; they are places where the scaffold currently *claims* more than it enforces. Shipping features on top of them means the claims stay false.

### 0.1 Enforce the consent gate server-side ⚠️ 🧠

- [x] Add a service function `assert_consent_valid(db, encounter_id)` that checks the ledger for a `given` event with no later `withdrawn` event for that encounter.
- [x] Call it in `confirm_upload` before setting `audio_object_key`, and again at the head of `transcribe_encounter`.
- [x] Return `409` (not `403`) when absent — this is a state problem, not a permissions problem.
- [x] Test: an encounter with no consent row must not be able to reach the pipeline.

⚠️ **Heads-up:** right now nothing server-side stops an encounter from being uploaded and transcribed with **zero** consent records. P0-1 says recording is blocked without consent, and today that rule lives only in the client — which is exactly where a legal control must *not* live, because the client is the part an attacker or a bug controls. This is the single most important gap in the current codebase.

📚 **Understand first:** the difference between a *UX guard* and an *enforcement point*. A greyed-out button is a UX guard. A server-side check that rejects the request is an enforcement point. Compliance controls need the second kind; the first kind is a courtesy. Every P0 requirement in the PRD that says "the app blocks X" should map to a specific server-side rejection you can point at in code.

🧠 **Your call — how strict is the re-consent rule?** P0-1 says a new participant mid-recording pauses recording until fresh consent is logged. Options: (a) the doctor flags it manually — simple, depends on the doctor remembering; (b) trust ASR diarization to detect a new speaker — automatic, but diarization invents and merges speakers constantly, so you'll get false pauses mid-consult; (c) manual flag now, revisit automation after you've seen real diarization output from your own clinic audio. My read is (c), because you have no Taglish diarization data yet and (b)'s failure mode interrupts a live medical exam. But this is a product-risk call, so make it deliberately.

### 0.2 Actually enforce RBAC ⚠️

- [x] Apply `require_role(...)` to routes. Right now it is defined in `app/api/deps.py` and used on **zero** endpoints.
- [x] Decide per-route: who can read a note? Only the authoring clinician, or any clinician in the clinic?
- [x] Test: a `compliance`-role token must not be able to `PATCH` a note; a `doctor` token must not be able to read the audit log.

⚠️ **Heads-up:** a dependency that is written but never attached is worse than one that doesn't exist — it reads like coverage in a code review and provides none. Grep for `require_role` before you trust the docstring in `models/clinician.py` that says role "drives" access control. It currently drives nothing.

### 0.3 Make auth survive a real clinic day 🧠

- [x] Add refresh tokens with rotation, or extend session lifetime deliberately.
- [x] Add an MFA enrollment endpoint (provision secret → return provisioning URI/QR → confirm with one valid code before activating). Today the TOTP secret can only be created by a seed script.
- [x] Add rate limiting on `POST /auth/login` (per-IP and per-email).
- [x] Add account lockout or exponential backoff after repeated failures.

⚠️ **Heads-up:** `ACCESS_TOKEN_EXPIRE_MINUTES=30` with no refresh path means a doctor gets logged out mid-consultation, roughly twice per clinic session. Discovering this in a pilot rather than now would poison the "voluntary use in week 4" metric for a reason that has nothing to do with note quality.

🧠 **Your call — where does the token live on the device?** Options: `expo-secure-store` (Keychain/Keystore-backed, survives app restart, the standard answer) vs in-memory only (safest against device compromise, forces re-login every launch). For a clinical app on a doctor's own device, secure-store plus a short-lived access token plus biometric re-auth on resume is the usual balance. Consider what happens to the token if the phone is lost — is there a server-side revocation list, or do you just wait for expiry?

### 0.4 Fix the type and consistency drift

- [x] Convert `Encounter.pipeline_status` from a free-form `String(32)` to a proper enum, the way `Note.status` already is. It currently accepts any string, and the codebase writes at least five different values across two files.
- [x] Move `confirm_upload`'s `audio_object_key` from a query parameter into a Pydantic request body.
- [x] Add a `CHECK` constraint or enum for `ConsentLedgerEntry.event` (`given|declined|withdrawn`).

📚 **Understand first:** why enums-at-the-DB-layer matter more here than in a typical CRUD app. Both `Note.status` and the consent ledger are *legal* records. "The database physically cannot hold an invalid value" is a much stronger statement to an auditor than "our code only ever writes valid values." That's the same reasoning behind the append-only trigger — push the guarantee as far down the stack as it will go.

### 0.5 Close the test-vs-production divergence ⚠️

- [x] Add a Postgres-backed test path (testcontainers, or a CI service container) for the tests that depend on Postgres-specific behavior.
- [x] Write a test proving the consent ledger rejects `UPDATE` and `DELETE`.

⚠️ **Heads-up — this one is sharp.** The test suite runs on SQLite via `Base.metadata.create_all()`. The append-only consent trigger lives in an Alembic migration. **Migrations never run in the test suite, so the trigger is never exercised by a single test.** I verified it manually with `psql`, which is why I know it works — but manual verification is not a regression test. Someone could drop that migration tomorrow and every test would still pass. Any guarantee implemented in SQL rather than Python is currently untested by construction.

📚 **Understand first:** "test against what you deploy." SQLite-for-speed is a common and often reasonable trade, but it silently voids every Postgres-specific guarantee: triggers, `pgcrypto`, native enums, `CHECK` constraints with Postgres semantics, concurrent-transaction behavior. Know exactly which of your guarantees fall in that blind spot, and cover those on real Postgres.

---

## Phase 1 — Make the pipeline real

Goal: audio recorded on a device ends up as a structured note in Postgres, with no human in the loop.

### 1.1 Upload path 🧠 📚

- [x] Implement an S3/MinIO client module (`app/services/storage.py`) — `boto3` is already a declared dependency and currently unused.
- [x] Implement chunked, resumable upload. Endpoints, roughly: `POST /encounters/{id}/upload/init` → `PUT /encounters/{id}/upload/chunk/{n}` → `POST /encounters/{id}/upload/complete`. (Presigned multipart shape: `POST .../upload/init` → `POST .../upload/parts/{n}` mints a presigned URL the device PUTs to directly → `POST .../upload/complete`.)
- [x] Persist per-chunk state so a resumed upload skips what already landed. (S3's own `ListParts` is the persisted state — `GET .../upload/parts` — rather than a mirrored Postgres table; see decision 0013.)
- [x] Enforce the idempotency key across the whole flow, not just encounter creation. (`upload/init` and `upload/complete` are both idempotent on retry — see docs/progress/1.1.)
- [x] Server-side encryption at rest on the bucket, plus a lifecycle policy keyed to `AUDIO_RETENTION_DAYS`.

🧠 **Your call — build the upload protocol or adopt one?** Three real options:
- **S3 multipart with presigned URLs.** The device uploads directly to object storage; your API only mints URLs and gets a completion callback. Cheapest to run, least bandwidth through your server, natively resumable. Cost: presigned-URL scoping is easy to get subtly wrong, and your API no longer sees the bytes (so it can't enforce anything about them).
- **[tus.io](https://tus.io) resumable protocol.** A real spec with mature client and server implementations, designed for exactly this. Cost: another moving part to run and understand.
- **Roll your own chunk endpoints.** Total control, matches the PRD's wording directly, and you'll understand every failure mode because you wrote them. Cost: you will reimplement bugs the other two already fixed — partial-chunk corruption, concurrent resume, orphaned uploads.

For learning value, rolling your own once is genuinely instructive. For a 4–8 week clinical MVP, presigned multipart is the pragmatic answer. If you roll your own, at minimum handle: chunk checksums, out-of-order arrival, and an orphan-upload reaper.

📚 **Understand first:** why idempotency keys exist at all. A phone on clinic wifi will retry a request whose response it never saw. Without a key, "retry" and "second consultation" are indistinguishable to your server, and you get duplicate notes on the same patient — a clinical-safety bug, not just a data bug. Trace the key's path through `encounters.py` and convince yourself where a duplicate could still slip through today.

⚠️ **Heads-up:** local audio must only be deleted after the server confirms *both* receipt and that note generation has begun (P0-2). Deleting on upload-complete alone means a server-side pipeline crash loses the consultation permanently. The confirmation the device waits for should be about the pipeline, not the bytes.

### 1.2 Transcript persistence 🧠

- [x] Add a transcript model/table (or object-storage document) holding: full text, per-word timings, per-word confidence, and speaker labels.
- [x] Make `transcribe_encounter` actually persist its output. It currently computes `segments` and discards them with `_ = segments`.
- [x] Make `generate_note` load the persisted transcript instead of passing `transcript=[]`.

🧠 **Your call — where does the transcript live?** Options:
- **Postgres `JSONB` column.** Queryable, transactional with the note, encrypted with your existing `EncryptedString` approach if you wrap it. Cost: word-level data for a 20-minute consult is large; you'll be loading megabytes to render one note.
- **Row-per-word table.** Precise, indexable by time, ideal for the grounding UI's "play from here." Cost: hundreds of thousands of rows per clinic-week and a heavier write path.
- **Object storage, like the audio.** Cheap, unlimited size. Cost: not queryable, another fetch on the read path, and a second place PHI lives that retention must remember to purge.

This choice largely determines how hard Phase 3 (grounding UI) is, so think about that requirement now rather than after. My instinct is `JSONB` for the MVP with the *sentence* as the addressable unit, because it keeps one transactional home for one note's data — but if you want word-precision audio seeking, the row-per-word table stops being overkill.

⚠️ **Heads-up:** the transcript is PHI, arguably more sensitive than the note (it's verbatim, including things the doctor chose not to record). Whatever you pick, it needs the same encryption, the same access logging, and the same retention clock as the audio. A retention job that purges audio and leaves transcripts is not a retention policy.

### 1.3 Real ASR integration ⚠️ 🧠

- [x] Implement `ElevenLabsScribeProvider.transcribe` — stream the object from storage, POST to Scribe v2 with diarization enabled. (Vendor changed to Groq-hosted Whisper large-v3, the user's call — no diarization capability at all as a result. See decision 0018.)
- [x] Handle rate limits, timeouts, and partial failures with Celery retries (already scaffolded via `self.retry`).
- [x] Record which ASR provider and model version produced each transcript.
- [x] **Fix `_parse_response`** — see the heads-up below. (N/A in the form asked: Whisper has no speaker labels to mis-group by, so the original bug's *mechanism* can't recur — but turn order is still explicitly preserved by construction; see docs/progress/1.3.)

⚠️ **Heads-up — there is a real bug in the stub I wrote.** `_parse_response` groups every word by speaker across the entire recording, producing one giant segment per speaker. That destroys turn order: you get "everything the doctor said" then "everything the patient said," instead of the actual back-and-forth. A note generated from that will mangle who reported which symptom. Segments must be *turns* — split when the speaker label changes. Worth reading that function and seeing the bug yourself before fixing it; it's a good example of code that looks reasonable and is semantically wrong.

⚠️ **Heads-up — superseded, kept for the reasoning.** This originally warned that Scribe returns anonymous `speaker_0`/`speaker_1` labels you'd have to map to doctor/patient yourself. That problem no longer exists in the form described here — the ASR vendor in use (Groq-hosted Whisper, decision 0018) has no speaker labels at all, anonymous or otherwise, so there's nothing to map. The actual heuristics below (doctor speaks first, doctor speaks the consent script, doctor has more speech time) are gone as a *mapping* tool but survive as a **content-inference** tool: if a diarization step is ever added back (decision 0018's options), or if Phase 1.4's note generation has to infer speaker roles from undiarized text alone, these are the same signals to reach for either way.

🧠 **Your call — how do you validate ASR quality with no bake-off?** The roadmap explicitly dropped the vendor bake-off and accepted this risk, making internal alpha the first real test. So decide now what you'll measure and how: a small set of consented recordings hand-transcribed as ground truth? Clinically-weighted entity error rate on drug names and doses (as the PRD's success metrics suggest)? Doctor-reported "did you have to fix a name/dose" flag on each note? Pick something cheap and start collecting from day one of alpha — the risk register says this surfaces via edit burden, and edit burden is only measurable if you instrumented it before the first note.

### 1.4 Real note generation ⚠️ 🧠

- [x] ~~Implement `LunaNoteGenerator.generate`~~ — **OBSOLETE.** Decision 0021 (2026-08-25, the user's call): Haiku is the sole note generator; Luna is deleted, not kept as a dormant fallback. `luna.py` no longer exists.
- [x] Implement `HaikuNoteGenerator.generate` — single fused call (P0-4), APSO section order, hedged language, silence/low-confidence suppression. (No longer "the configured fallback" — it's the only provider. See decision 0021 for what that costs: P0-4's fallback-as-risk-mitigation no longer exists.)
- [x] Use structured output (JSON schema / tool call), not free-text parsing. (A forced `tool_choice`, not a hopeful one — the model cannot answer any way but the schema.)
- [x] Pass word-level confidence into the prompt in a form the model can act on. (Not "act on" as in "please be careful" — words below `NOTE_GENERATION_LOW_CONFIDENCE_THRESHOLD` are physically replaced with `[INAUDIBLE]` before the prompt is built, per the heads-up below.)
- [x] Store the prompt version alongside each generated note. (`Note.prompt_version`, `b8c9d0e1f2a3_note_prompt_version.py`.)
- [x] Add a golden-transcript test suite: fixed transcript in, assertions on the note out. (`tests/test_note_generation_haiku.py::test_generate_end_to_end_with_a_mocked_response`.)

⚠️ **Heads-up:** "generation is suppressed over silent or low-confidence windows" (P0-4) will not happen just because your system prompt says so. Models are strongly biased toward producing fluent, complete-looking clinical text. If you hand it a transcript with a garbled 30-second stretch, it will smooth over the gap plausibly and you will not be able to tell. Make suppression *mechanical* where you can: mark low-confidence spans in the input explicitly (e.g. `[INAUDIBLE 0.31]`), and validate the output for invented content rather than trusting instruction-following. **Followed, two layers deep:** `_format_transcript` replaces any word below `note_generation_low_confidence_threshold` with a literal `[INAUDIBLE]` before the model ever sees it (the model cannot smooth over a gap it was never shown), and the schema forces the model to set a per-section `suppressed` boolean explicitly rather than letting the code infer it from empty text — and `suppressed=true` forces `text=""` server-side even if the model inconsistently also emitted sentences.

⚠️ **Heads-up:** storing the prompt version per note matters more than it sounds. When edit burden jumps in week 3, the first question is "did we change the prompt?" — and without a version stamped on each row, that question is unanswerable after the fact. **Followed:** `PROMPT_VERSION = "haiku-v1"` in `haiku.py`, stored on `Note.prompt_version` on every generation — bump the constant whenever the system prompt or tool schema meaningfully changes.

🧠 **Your call — how do you get trustworthy source spans?** P0-4 requires every generated line to trace back to its transcript passage, and P0-7 builds a UI on that. But **an LLM asked to emit character offsets will produce confident, wrong numbers** — it cannot count characters reliably. Options:
- Have the model **quote** the exact supporting passage verbatim, then string-search the transcript server-side to compute real offsets. Slower, more tokens, but the offsets are ground truth.
- Give each transcript sentence a stable **ID** in the prompt and have the model cite IDs. Cheap, robust, coarser granularity.
- Ask for offsets directly. Cheapest, and I'd expect it to be wrong often enough to break the feature.

I'd take sentence IDs: it's the only option that's both cheap and verifiable, and sentence-level grounding is almost certainly enough for a doctor scanning for "where did this come from?"

**Resolved (implementation-level judgment call, decision 0022):** segment IDs — but the *already-persisted* transcript segment ID from Phase 1.2 (decision 0016), not a new sentence-numbering scheme built just for this. The model cites `segment_ids` in its tool-call output; any ID that doesn't match a segment actually sent in the prompt is dropped before the note is saved, not trusted (`HaikuNoteGenerator._build_section`). `text_start`/`text_end` — offsets into the note's own generated text, not the transcript — are never asked of the model at all; the server computes them exactly, by tracking a cursor while concatenating the model's own per-sentence output.

📚 **Understand first:** the difference between a model *citing* and a model *appearing to cite*. Any output can contain a plausible-looking reference. Only a reference you independently verified against the source is evidence. This distinction is the whole reason the grounding UI is a P0 requirement rather than a nice-to-have — it's the doctor's mechanism for catching exactly this failure.

### 1.5 Pipeline failure handling

- [x] Give the doctor a specific, actionable error state per failure mode (upload failed / transcription failed / generation failed) — the PRD's edge case explicitly rejects "a silent gap in the record." *(Done, minus `upload_failed` — decision 0023 explains why that one specifically doesn't need a persisted state. `TRANSCRIPTION_FAILED`/`GENERATION_FAILED` are real `EncounterPipelineStatus` members now, plus `retry_count`/`last_pipeline_error` on `EncounterOut` so a client has an actual message, not just a state name.)*
- [x] Add a dead-letter path: after max retries, mark the encounter failed and surface it in the app. *(Done — `_mark_stage_failure` in `app/tasks/pipeline.py`; surfaced via `GET /encounters/failed` since there's no app yet to surface it in.)*
- [x] Never leave an encounter stuck in an intermediate `pipeline_status` with nothing watching it. *(Done — `sweep_stuck_encounters`, Celery Beat, every 5 min. See the resolved 📚 note below: this is deliberately a second, separate mechanism from dead-lettering, not the same one applied twice.)*
- [x] Add a "regenerate note" action for transient failures. *(Done, generalized to both failure stages — `POST /encounters/{id}/retry` re-runs only the stage that failed, not the whole pipeline; see decision 0023 for why re-transcribing on a `GENERATION_FAILED` retry would be wasted real money.)*

📚 **Understand first:** a queue system's real hard part isn't throughput, it's *stuck work*. Every async pipeline eventually has jobs that neither succeeded nor failed loudly. Decide now how you'll notice: a periodic sweep for encounters older than N minutes in a non-terminal state is the usual answer, and it's much easier to add before you have production data than after.

**Followed, and worth being explicit about why it's not the same code path as dead-lettering (decision 0023):** dead-lettering only fires for a task that actually ran and raised an exception. It structurally cannot catch a task that never ran at all — broker down, or the worker pool at zero, at the exact moment the pipeline was kicked off. `sweep_stuck_encounters` catches that other case by comparing a `pipeline_updated_at` timestamp against a configurable staleness threshold instead of by catching anything — there's nothing to catch. Re-kicking a merely-slow (not actually stuck) encounter is safe because both tasks were already idempotent no-ops on already-done work, from Phase 1.2/1.3 — the sweep only has to be safe when wrong, not perfectly accurate about what's really stuck.

---

## Phase 2 — Build the mobile app

The largest phase, now complete. A doctor can go from consent to signature in one pass.

### 2.1 App foundation

**Client re-platformed to a browser web app on a clinic laptop — decision 0024.** The supervisor answered the PRD's open question ("what devices do doctors actually carry": laptops), which collapsed both reasons `tech-stack.md` gave for rejecting a web client. Items below are rewritten accordingly; `apps/mobile/` is deleted (git history retains it), `apps/web/` replaces it.

- [x] ~~Navigation (`expo-router` or React Navigation)~~ → **React Router**, `apps/web/src/App.tsx`, with an auth-gated route split and a `checking` state so a reload never flashes the login screen at an already-signed-in doctor.
- [x] Generate a typed API client from FastAPI's OpenAPI schema rather than hand-writing fetch calls. This is the payoff for the Python-backend/TypeScript-client split described in `docs/tech-stack.md`. (**Done, and it survived the re-platform untouched — the strongest item in this phase.** `openapi-typescript` + `openapi-fetch`; 1583 lines of types generated from the live schema's 22 paths / 31 components into `src/api/schema.d.ts` via `npm run api:types`. A renamed backend route now breaks `tsc`, not a clinic.)
- [x] Auth flow: login → TOTP → secure token storage → ~~biometric unlock on resume~~. (Login is single-step: the API takes email + password + TOTP together and returns the same 401 whichever factor failed. Token storage is *better* than the mobile plan could manage — see the Understand-first note below. **Biometric unlock is deliberately deferred, not done — see the open question below it.**)
- [x] Error/offline UI primitives you'll reuse everywhere. (`components/Banner.tsx`, `ErrorBoundary.tsx`, `lib/offline.ts` — persistent banners, not toasts, because a toast that vanishes in three seconds *is* silent failure for a doctor who was looking at a patient.)
- [x] ~~Build a real dev client (`npx expo run:android`)~~ — **OBSOLETE, and this is the phase's biggest saving.** This item existed solely to host the native audio modules a phone needed for background capture. With a laptop it does not exist rather than being solved.
- [x] **NEW — CORS + httpOnly refresh cookie on the API.** Two things a browser client needs that a native one never did, and both fail *silently*: without CORS the preflight is rejected and the request never reaches a route, so nothing appears in the API log at all. Covered by 10 tests in `tests/test_web_client_support.py`.

⚠️ **Heads-up — superseded, kept for the reasoning.** This warned that the Android/iOS toolchain is "a genuine time sink... the most common place a mobile-first plan slips." Still true of mobile, and now moot: there is no toolchain, no store, no signing, and no Mac requirement. Worth keeping visible because it is *why* the re-platform paid for itself immediately — and because it returns in full if the laptop premise ever fails (decision 0024's "what would change my mind").

🧠 **Your call — biometric unlock on a *shared* laptop.** The original item assumed a personal phone, where biometric unlock is unambiguous. A shared clinic laptop breaks that assumption: Windows Hello / Touch ID via WebAuthn authenticates *the machine's logged-in user*, so if several doctors share one Windows session, a biometric prompt proves nothing about which doctor is signing a note. Options:
- **Per-doctor OS accounts**, after which WebAuthn works as intended. Cleanest, but an IT policy decision rather than a code one.
- **Short idle-lock requiring password + TOTP re-entry.** Weaker UX, correct on a shared login, needs no WebAuthn at all.
- **WebAuthn regardless**, accepting that it identifies the device rather than the clinician — which is arguably worse than nothing, because it *looks* like proof of identity.

I'd want to know whether doctors share a Windows login before building any of them; the answer decides which is even coherent. Deferred rather than guessed, and nothing else in Phase 2 depends on it. Note this interacts with signing (2.6), where "who signed this note" is a medico-legal question, not a UX one.

📚 **Understand first — why the token storage got *better*, not worse.** The mobile plan put the refresh token in `expo-secure-store`, which is readable by app code and therefore by anything achieving code execution in the app. The browser has something the phone did not: an **httpOnly** cookie, which JavaScript cannot read at all. So the access token stays in memory (never `localStorage`) and the refresh token lives in a cookie this codebase cannot see. The payoff shows up in the flow: after a full page reload there is no access token, the client calls `/auth/refresh` with an *empty body*, and the **server** reads the cookie — which is how "resume after reload" works without ever persisting a credential anywhere JS can reach. Decision 0006 is amended, not reversed.

⚠️ **Heads-up — a client-side hazard created by Phase 0.3's own security.** Refresh tokens rotate on every use, and a replayed one is treated as theft and revokes the whole session family. So two *concurrent* refreshes are self-harm: the second presents a token the first already rotated, reuse detection fires, and the client logs the doctor out mid-consultation. The fix is a single shared in-flight promise so N concurrent 401s produce exactly one refresh (`refreshSession` in `src/api/client.ts`). This is not an optimization, and it is invisible until you have two parallel requests — which an upload queue guarantees.

### 2.2 Recording — the hard part 📚 ⚠️ 🧠

- [x] One-tap record with a persistent, always-visible recording indicator (P0-1). (`components/RecordingIndicator.tsx` — sticky, undismissable, `role="status"`, and it surfaces missing audio inline rather than in a details panel. It is a legal control under RA 4200, not a status chip.)
- [x] Background capture that survives app backgrounding, screen lock, and incoming calls (P0-2). **Re-scoped to the laptop and partly already measured (decision 0024):** backgrounding and screen lock are *empirically satisfied* — 0.05s of audio lost across 131s hidden over 9 windows. "Incoming call" is essentially N/A. **Lid close / system sleep is not satisfied and is unsatisfiable in software** — it cost 6.5s of real audio in the harness run, and it is OS power policy that neither a browser nor Electron can veto. Mitigation is device config (Windows: "When I close the lid → Do nothing") plus chunked IndexedDB writes so a suspend truncates rather than destroys.
- [x] ~~Android: foreground service with an ongoing notification.~~ **OBSOLETE** — no Android app (decision 0024).
- [x] ~~iOS: `UIBackgroundModes: audio` plus correct `AVAudioSession` category.~~ **OBSOLETE** — no iOS app (decision 0024).
- [x] Encrypt audio on-device *before* it touches disk, key sealed in ~~Keychain/Keystore~~ **a non-extractable Web Crypto `CryptoKey` in IndexedDB**. (Verified, not assumed: the end-to-end test reads the browser's own IndexedDB and asserts the WebM magic bytes are absent from what was stored — if plaintext audio ever reaches disk, that check fails.) Note the honest downgrade this represents: a browser cannot seal a key in a hardware keystore the way Keychain/Android Keystore could. If Legal requires hardware-sealed key custody for on-device PHI, decision 0024's option (b) — an Electron wrapper — becomes necessary, and this is the item that forces it.
- [x] Handle interruptions: ~~pause/resume on phone call~~, **save partial audio on crash**. (Crash-safety done: 5s chunks land in IndexedDB as they go, so a crash or suspend costs at most one chunk, and `requestData()` flushes MediaRecorder's partial chunk on stop — without it the tail of the consultation, often where the plan is stated, is discarded. "Phone call" is N/A on a laptop; a *deliberate* pause for a mid-visit interruption is a real need and belongs with 2.3's re-consent flow, which is what makes pausing legally meaningful.)
- [x] Write audio in chunks as you go, never buffering a whole consult in memory. (~5s / ~20 KB chunks — deliberately *unrelated* to S3's 5 MB minimum part size, which at 32 kbps is ~21 minutes of audio. Sizing chunks to the S3 minimum would risk 21 minutes per crash, the opposite of what a write-ahead log is for. See decision 0026.)

- [x] **NEW — client-side consent gate (P0-1).** P0-1 says the app must block recording *"before anything is captured"*, and the existing server enforcement (upload confirmation, transcription) both run *after* capture. New `GET /api/v1/consent/{encounter_id}`, built on the same ledger fold `assert_consent_valid` uses so the read and the enforcement cannot disagree — asserted directly across five ledger sequences. Fails closed on every uncertain path including offline, and re-checked at the moment of the tap, not only on mount. See decision 0026 for the cost of the offline choice.

⚠️ **Heads-up — audio gaps are now *recorded*, because they cannot be prevented.** Decision 0024 measured lid-close costing 6.5s of real audio, and established that OS power policy beats any client architecture. Given that, the only honest design is to detect the loss and say so: an AudioWorklet counts samples as ground truth (codec-independent, and unlike byte-counting it is not fooled by Opus encoding silence to nearly nothing), wall-clock jumps are read as suspends, worklet silence as stalls, and missing time appears **in the recording indicator itself**. Note the anchoring subtlety, which was a real bug in the harness before it was fixed: the measurement starts at the first worklet message, not the button press — the ~0.7–1.3s between them is startup latency, and charging it to "missing audio" produced a false loss warning on a perfectly healthy run.

📚 **Understand first:** mobile OSes treat background microphone access as adversarial by default, and reasonably so. Both platforms will kill or silence a backgrounded recorder unless you declare intent through a specific mechanism — a foreground service on Android, a background mode on iOS — and both require user-visible indication. This is precisely why `docs/tech-stack.md` rules out a PWA: browsers give you no such mechanism at all. Read the platform docs for background audio before writing this; it's the difference between a day and a week.

⚠️ **Heads-up — OBSOLETE.** "Apple review will ask why a medical app records in the background" — there is no App Store submission (decision 0024). Kept because the underlying point survives the platform change: the clinical justification for recording still needs writing down, and the consent flow is still the thing that satisfies whoever asks. That is now Legal and Remedy's DPO rather than Apple.

⚠️ **Heads-up — mostly defused, not fully.** This warned about battery/thermal over a clinic day on a mid-range Android. The PRD's open question is now answered (laptops, decision 0024) and a plugged-in laptop removes most of the risk — and at mono Opus 32 kbps a 30-minute consult is ~7 MB, not a big file. What remains genuinely unmeasured: an actual 8-hour day of intermittent recording on the real clinic laptop. "Far less pressing" is not "measured".

🧠 **Your call — audio format and bitrate.** You're trading ASR accuracy against file size against upload time on clinic wifi. Speech at 16 kHz mono in a compressed format (AAC/Opus) is usually plenty for ASR and dramatically smaller than uncompressed WAV. But verify against Whisper's documented input expectations before committing (Groq's hosted large-v3, decision 0018 — not Scribe, since the ASR vendor changed in 1.3), and test whether aggressive compression measurably hurts Taglish accuracy — code-switched speech may be less robust to compression artifacts than monolingual English.

**Resolved (decision 0025, the user's call): mono Opus at 32 kbps.** Brought forward from 2.2 into 2.1 because the capture harness produced a number worth reacting to before any recorder existed: its 29-minute run recorded **129 kbps stereo, 26.7 MB**, having silently ignored a `channelCount: 1` constraint. That is roughly 550 MB per clinic day over clinic wifi versus ~115 MB at the chosen setting, for no accuracy gain — Whisper resamples to 16 kHz mono internally regardless. Constants live in `apps/web/src/lib/audio-config.ts`. Critically the implementation does not *trust* the constraint: it also sets `audioBitsPerSecond` explicitly (without which the browser picks, which is exactly how 129 kbps happened) and calls `assertAudioSettings()` to surface any requested-vs-actual mismatch loudly rather than silently.

### 2.3 Consent flow (P0-1)

- [x] Consent screen presenting the script in Filipino and English, blocking recording until resolved. (`routes/Consent.tsx`. **The script text is a placeholder written by an engineer and is not cleared by counsel** — the app says so on screen, and the text is isolated in `lib/consent-script.ts` so counsel's version is a single edit. RA 4200 clearance is the PRD's own blocking open question.)
- [x] Capture the participant roster before recording starts. (Doctor and Patient locked as always-present, extras opt-in — RA 4200 needs the consent of *every* party, so each is named on the ledger entry.)
- [x] Record the spoken consent exchange as the first segment of the audio file. (**Read P0-1's two bullets together before touching this** — see the 📚 note below. The ordering is forced: log consent, *then* start recording, then speak the confirmation. The consent screen never touches the microphone.)
- [x] Handle decline gracefully — the app must remain fully usable without recording (explicit PRD edge case). (And decline is deliberately unreachable until the script has been presented: logging a decline the patient was never read would claim an informed refusal that did not happen.)
- [x] Mid-visit re-consent: pause recording, capture new roster, log a new ledger entry, resume. (Manual flag — **decision 0003 is now closed by elimination**, since decision 0018 removed diarization entirely. The pause happens *before* any network call because it is the compliance action; resuming is gated on the ledger write succeeding, not on the doctor's word. See the ⚠️ below for the three ways pause interacts with 2.2's gap detection.)
- [x] Withdrawal action, available at any time, that reaches the server. (Client: stop capture → delete local chunks → tell the server, so a failed network call leaves *less* data behind, not more. Server: ledger entry committed first, retention clock set to now as the durable backstop, then a best-effort immediate object delete.)

⚠️ **Heads-up — addressed, and the advice was followed exactly.** This warned that withdrawal had *no* server-side effect, and that the honest design is "stops at the next checkpoint" rather than "stops instantly". Both now hold: `handle_withdrawal` sets the retention clock to now (durable backstop) and attempts an immediate object delete (best-effort), while the pipeline stop relies on Phase 0.1's consent re-checks at upload confirmation and at the head of `transcribe_encounter`. **No attempt is made to kill a running Celery task**, and the UI wording says "next stage boundary, not instantly" — asserted by a smoke check, because that sentence is also what Legal will be told the system does.

📚 **Understand first — P0-1's first two bullets constrain each other, and reading either alone gets it wrong.** Bullet 1 says the script is presented *"before anything is captured"*. Bullet 2 says that once consent is given, *"the spoken exchange is captured as the first segment"*. The tempting reading — start recording, read the script, and the recorded asking becomes segment 1 — satisfies bullet 2 and **violates bullet 1**. The only sequence satisfying both is: roster → read the script → log the outcome → start recording → speak a short confirmation, which becomes segment 1. A consequence worth being explicit about with Legal: the patient's own spoken "yes" is therefore *not* on the recording, only the doctor's confirmation that it was given. Putting it on tape would require recording before consent is logged, which is a decision someone with authority has to make.

⚠️ **Heads-up — pausing is not just `MediaRecorder.pause()`; it collides with 2.2's gap detection three ways.** Each would have produced a confidently wrong reading: (1) the AudioWorklet keeps counting samples the recorder is no longer writing, so the pause reports as *lost audio* — fixed by suspending the `AudioContext` too, stopping both clocks together; (2) the stall detector fires, because worklet silence is its symptom and during a deliberate pause that silence is expected — so the monitor skips stall/suspend detection while paused; (3) the pause duration reads as a wall-clock jump, i.e. a "system suspend" gap — the recorder logging a fault it caused itself — so both watchdog baselines reset on resume. And one that silently eats audio: **a paused `MediaRecorder` ignores `requestData()`**, so `stop()` must resume before flushing or the buffered tail is discarded.

### 2.4 Offline queue (P0-2)

- [x] Durable queue surviving app kill and device restart — **IndexedDB**, not `expo-sqlite` (decision 0024). Same write-ahead-log invariant, different store. (Schema v2, `uploads` store beside the audio chunks — one database, one version, one guarded additive upgrade handler so a laptop mid-pilot upgrades without losing queued audio. A crashed recording is recovered on next launch and uploaded: partial audio beats none.)
- [x] Visible, persistent queue status — nothing may fail silently (explicit PRD requirement). (`components/QueueStatus.tsx`, on both the recording screen and the worklist. **Two bugs in this readout were found by the end-to-end test's own output rather than its assertions** — see the ⚠️ below; both passed the original suite while being visibly wrong in the log.)
- [x] Background upload with exponential backoff. (5s doubling, capped at 5 min, **jittered** so several laptops recovering from one wifi outage do not hit the API in synchronised waves. Crucially, `OfflineError` does *not* consume the attempt budget — counting an outage toward the retry ceiling would dead-letter healthy recordings during exactly the event this queue exists to survive.)
- [x] Generate the idempotency key on-device at recording start, and persist it before the first byte is uploaded. (Asserted directly end-to-end: at t+0.9s the queue entry exists in state `recording` with its key persisted, and **zero chunks are on disk** — the record of intent genuinely precedes the data.)
- [x] Delete local audio only after the server confirms receipt *and* pipeline start. (A distinct `uploaded → confirmed` step polls `GET /encounters/{id}` and advances only at `transcribed`/`note_generated`. See the 📚 note below for why the 200 on `upload/complete` is not enough. A *terminal server failure* keeps the local copy — it may be the only one.)
- [x] Handle the device-full case. (Checked *before* recording starts, because a `QuotaExceededError` halfway through a consultation loses the rest of it with no graceful recovery. Reported as **minutes of recording remaining**, not a percentage: "8% free" means nothing to a doctor, "about 20 minutes left" is directly comparable to a consultation.)

📚 **Understand first:** this is a write-ahead log, the same pattern databases use for durability. The invariant is that the *record of intent* is committed to durable storage before the risky operation begins, so a crash at any point leaves you able to reconstruct what should happen. If you generate the idempotency key in memory and crash before persisting it, the retry generates a new key and you get a duplicate — which is exactly the bug the key exists to prevent.

📚 **Understand first — "receipt" and "pipeline start" are different events, and only one of them is a 200.** `upload/complete` returning 200 confirms that S3 holds the object and a Celery chain was *enqueued*. It says nothing about whether a worker ran. A broker outage or an empty worker pool — the precise scenario Phase 1.5's stuck-sweep exists for — leaves `pipeline_status` at `uploaded` indefinitely, and deleting on the 200 would destroy the only copy of a consultation whose processing never began. So the queue has a separate `uploaded → confirmed` step that polls the encounter and advances only at `transcribed`, which is both the first status proving work happened *and* the point the transcript exists server-side, so audio stops being the sole record of what was said. The mirror case matters too: a *terminal* server failure (`transcription_failed`, `blocked_no_consent`) must **keep** the local audio, since it may be the only copy and Phase 1.5's `/retry` can still use it.

⚠️ **Heads-up — the queue's status readout is the easiest thing here to get quietly wrong.** Two bugs were caught by reading the end-to-end test's own output, not by its assertions; both left the upload working while making the status lie, and the status *is* the P0-2 requirement. (1) `recoverInterrupted` ran every tick and could not distinguish "the app crashed mid-recording" from "recording is happening right now in this tab" — so a normally-stopped 14-second recording was labelled *"Recording was interrupted"* and queued for upload **while still capturing**, risking chunks written after the upload being deleted unsent. Fixed with a heartbeat plus a staleness window; a timestamp rather than an in-memory flag, because it must survive the process dying, which is the case being detected. (2) The byte total was taken from React state captured *before* `stop()` flushed MediaRecorder's final chunk, so the panel showed *"56 KB of 37 KB"* — progress over 100%. Now derived from the chunk store, which is the only thing that knows what is on disk. Both have regression assertions; neither would have been noticed by a passing suite.

### 2.5 Patient identity (P0-6)

- [x] Patient search by typed **or dictated** name. (Typed search done — `GET /patients/search`, name-only and fuzzy. **Dictation is not implemented**: the Web Speech API would cover it, but its accuracy on Taglish names is exactly what needs measuring before being trusted with patient identity. Flagged, not silently dropped.)
- [x] Exact match links silently; near match requires one-tap confirmation; no match offers create-new. (**"Silently" applies only to a single exact hit** — two patients with an identical name is precisely where silence attaches the note to the wrong person, so that falls through to confirmation. Candidates always show birthdate, since that is what tells similar names apart.)
- [x] Loose-sessions tray for recordings started before a patient was selected. (On the worklist, with the one-tap linking action. Asserted end-to-end that recording *starts* with no patient linked — P0-6's "never blocked on identity" is a property worth testing, not assuming.)
- [x] Re-confirm patient identity at the moment the note is filed, not only at recording start. (Filing now requires the caller to **name** the patient, and the server checks it against the encounter's own `patient_id`. Reading that field and trusting it would make this a formality — a stale client showing the previous patient is rejected with 409 rather than silently corrected. Filing is the last cheap moment to catch a mis-linked recording.)

⚠️ **Heads-up — an architectural consequence you'll hit immediately.** `Patient.full_name` is encrypted at rest via `EncryptedString`, which means **the database cannot search it.** Ciphertext isn't comparable, so `match_patient` filters by exact birthdate first and only then compares names in Python. The PRD's UX wants name-first search — and name-first search over encrypted columns is impossible without help.

🧠 **Your call — how do you get searchable encrypted names?** Options:
- **Blind index:** store an HMAC of the normalized name alongside the ciphertext, and search that. Enables exact and prefix matching while the name stays encrypted. Standard solution; adds a second key to manage, and leaks equality (you can tell two patients share a name without decrypting).
- **Don't encrypt the name;** rely on DB-level and disk-level encryption instead. Simplest, searchable, weaker — a Postgres read is now a PHI read.
- **Keep birthdate-first matching** and design the UX around it (front-desk check-in queue, PRD's own P1 item, sidesteps search entirely).

**Resolved (decision 0029) — and the blind index above does not actually fit.** P0-6's entry point is a typed or dictated name that must **fuzzy-match**, and an HMAC supports *equality only*: you cannot compute a similarity ratio against a hash, so "Maria Cruz" for *Maria Santos Dela Cruz*, or "Cruzz" for "Cruz", returns nothing. Prefix matching is not really available either, since HMAC-of-a-prefix ≠ prefix-of-an-HMAC.

So names are decrypted and ranked in Python. **That was measured, not assumed** — the naive version took **2.1 seconds at 5,000 patients**, and the breakdown redirected the fix entirely: raw `SELECT` of ciphertext 7.7 ms, decrypting every value 118 ms, but a full **ORM** query 348 ms and unfiltered `difflib` 183 ms (68 ms with a token prefilter). **Decryption is not the bottleneck; ORM hydration and unfiltered similarity are.** The implementation uses a raw three-column `SELECT` plus a shared-token/prefix prefilter — ~194 ms at 5,000 patients, roughly 10× better.

The scale ceiling is recorded as numbers in decision 0029, along with the correct next step if the directory outgrows it: a **token-level** blind index (an HMAC per name token), which lets SQL do the prefilter while *preserving* fuzzy matching — strictly better than the whole-name index proposed above.

Also worth thinking through: a mistyped birthdate currently means dedup silently fails and you create a duplicate patient — which puts one person's history in two records. What's your recovery path? A merge tool is unglamorous and you will need it.

### 2.6 Review, edit, sign (P0-5)

- [x] Note review screen, Assessment → Plan → Subjective → Objective order. (APSO, **not** SOAP — P0-4 specifies it because the doctor's own conclusion is what they check first, and burying it under recounted symptoms is how a wrong assessment gets signed. Asserted end-to-end by reading the rendered heading order.)
- [x] Free editing of any section pre-signing, with each edit recorded as a `NoteRevision`. (Saved **on blur**, one section at a time, not on a single Save at the end: a metric computed from one final diff cannot distinguish "the draft was nearly right" from "the doctor rewrote it in one pass", and edit burden is the pilot's headline target.)
- [x] Explicit stepwise state transitions — no skipping (server already enforces this). (The UI offers exactly one next step; the server remains the enforcement point, and the end-to-end test confirms signing straight from `generated` is rejected with 409 rather than the UI merely hiding the button.)
- [x] Signing ceremony: deliberate, distinct, capturing name + PRC license + timestamp. (Visually and structurally separated from the ordinary next-step button, requires typing the licence every time, and says plainly that it transfers accountability from the model to the clinician. It must not look like a Save button. Signed notes are then immutable — 409 on edit.)
- [x] Objective-findings entry for things never spoken aloud. (The Objective section, with a hint saying exactly that — the recording cannot know what was observed silently.)
- [x] Show the prior visit's assessment and plan for longitudinal context. (**Only signed notes count**: presenting an unsigned note as "the last visit" would hand the doctor an unreviewed AI draft as established history — the opposite of what signing is for. Subjective/objective are excluded because last visit's symptoms are not today's.)

🧠 **Your call — what counts as a "minor edit"?** The PRD's headline quality target is "≥70% of signed notes require only minor edits," so this definition literally determines whether you pass. Character-level edit distance? Word-level? Clinically-weighted (a changed dose counts more than a rephrased sentence)? Decide before alpha, write it down, and compute it consistently — a metric redefined mid-pilot tells you nothing.

**STILL OPEN — deliberately deferred to Phase 6, not guessed.** 2.6 does not need the definition, and picking one here would bake a measurement choice into a UI phase. What 2.6 *does* guarantee is that the definition stays free: `NoteRevision` stores full before/after text for **every** edit, so any candidate definition — character distance, word distance, clinically-weighted — can be computed retrospectively over the same data. The checklist's warning stands: it must be written down before alpha, because a metric redefined mid-pilot tells you nothing.

⚠️ **Heads-up — a feature can be complete and still be dead code.** Writing 2.6's end-to-end test surfaced that the review screen was **unreachable**: notes are 1:1 with encounters, but nothing exposed the note id, so `/notes/{id}` had no route into it from any worklist. The test could not navigate to the screen it existed to exercise. Fixed by adding `note_id` to `EncounterOut`. Worth remembering as a shape — unit tests and a typecheck will both pass on a screen no user can get to.

⚠️ **Heads-up:** `NoteRevision` stores full before/after text per edit. Every revision is another encrypted copy of PHI, and they compound fast. Make sure retention covers revisions too, and consider whether you need every keystroke-level revision or just per-save snapshots.

---

## Phase 3 — Grounding UI (P0-7)

- [x] Tap a note line → highlight the source transcript passage. (Sections render as **clickable lines** by default and swap to a textarea on an explicit "Edit this section" — you cannot click a line inside a textarea, and making verification the default gesture is the point rather than a workaround. A line citing **nothing** is marked in the note itself, since that is the line most worth a second look.)
- [x] Tap again → play audio from that timestamp. (Two taps exactly as specified: playing a recording out loud is not something to trigger by accident in a room with a patient in it. Playback plays a **window, not a file** — it stops at the cited passage's end rather than running on through the consultation, with a 250 ms tail so the ASR's last-word timestamp does not clip the final word.)
- [x] Serve audio to the device without permanently re-downloading PHI (short-lived presigned URLs, range requests). (Both halves enforced rather than hoped for: the URL is signed `Cache-Control: no-store` and `Content-Disposition: inline`, so the bytes reach neither the browser cache nor the Downloads folder; Range requests go straight to object storage, so only the seconds played transfer and the API server never sees the audio. **HTTP 206 asserted end-to-end.** Minted on demand from its own endpoint — a presigned URL is a live playable handle on PHI, and issuing one on every note open hands out a working link to a recording the doctor may never ask to hear.)
- [x] Handle the case where audio has already been deleted by retention but the note remains. (**And the harder case the heads-up implies:** the bucket's own lifecycle rule deletes recordings with nothing writing back to the encounter row, so `audio_object_key` set and `audio_deleted_at` NULL is *not* evidence the bytes exist. Verified with a `HEAD` before any play button is offered, then the row is corrected. Five states, not two — available, never-recorded, **withdrawn** (at the patient's request, a legal event, not the passage of time), expired, and **unreachable**, which is deliberately not rounded up to "deleted".)

📚 **Understand first:** this feature is the product's trust mechanism. The doctor's rational response to "an AI wrote this" is "prove it," and grounding is the proof. Everything upstream — span storage, sentence IDs, verbatim quoting — exists to make this screen honest. If you cut corners in 1.2 or 1.4, this is where it shows.

⚠️ **Heads-up:** notes outlive audio. Retention will delete the recording while the signed note is a permanent medical record. The grounding UI must degrade gracefully to "transcript only" and then to "source no longer retained" — and the doctor should understand which state they're in, not just see a dead play button.

---

## Phase 4 — Security and compliance hardening (P0-8)

### 4.1 Encryption and key management 🧠 ⚠️

- [x] Decide and document the PHI-at-rest approach. (Decision 0031: **app-layer Fernet stays.** `pgcrypto` puts the key into the SQL statement itself — and therefore into `pg_stat_statements` and any query log — costs the fast test suite its database, and **does not solve rotation anyway**, since it carries no key id either. KMS envelope encryption is the right long-term answer; it buys cheap rotation, which currently costs three minutes. **The final call is explicitly the DPO's**, and the argument for asking now is that the migration is a re-encryption pass whose runtime is a measured number rather than a guess.)
- [x] Key rotation procedure, written and rehearsed — **rehearsed for real, not described.** (`MultiFernet` over `PHI_ENCRYPTION_KEY` plus a decrypt-only `PHI_ENCRYPTION_KEY_PREVIOUS`, and `scripts/rotate_phi_key.py`, which **discovers encrypted columns from SQLAlchemy metadata by type rather than from a list** — a hand-maintained list is how you permanently lose one column. Rehearsed against real Postgres at **17,500 rows / 35,000 encrypted values / 1.73 GB: 181.0 s**, then verified 35,000/35,000 readable under the new key alone and 0/35,000 under the old. Two surprises: **cost is bytes, not rows** — transcripts are 14% of values and 91% of the time, so rotation cost is O(recorded minutes retained), which makes 4.4's deletion job a rotation-cost control — and **crypto is only 15% of it** (decrypt 16.5 s, +re-encrypt 26.7 s, +`UPDATE` 181.0 s), so neither `pgcrypto` nor a KMS would make rotation meaningfully faster. Second time on this codebase a measurement has moved blame off decryption; see 0029.)
- [x] Separate keys per environment; production keys never on a developer machine — **enforced at boot, not documented as a rule.** (The development key is now *published on purpose* in `.env.example` and denied by fingerprint, so production refuses to start on a known repo secret. Also refused: a missing or malformed key, the same key listed as both current and previous, `REFRESH_COOKIE_SECURE=false`, and a localhost CORS origin. All problems are reported at once rather than one per restart. Verified live — `ENVIRONMENT=production` with the published key exits 1 listing all four.)
- [x] TLS everywhere, HSTS, modern cipher suites — **the application half only, and the runbook says so rather than letting green headers imply otherwise.** (Raw ASGI middleware — not `BaseHTTPMiddleware` — setting HSTS, a `default-src 'none'` CSP, `nosniff`, `DENY`, `no-referrer`, COOP, and `no-store` on `/api/v1`. HSTS is deliberately suppressed over plain-http dev, because it pins localhost across *every port* for two years and would break unrelated local work. ⚠️ **Phase 5 still owes the transit half**: TLS termination, http→https redirect, a TLS 1.2 floor with modern ciphers, TLS to Postgres/Redis/object storage, and `X-Forwarded-Proto` set by the proxy *and settable by nobody else* — the app trusts that header to decide whether to emit HSTS.)

⚠️ **Heads-up — the current setup has no rotation story and a single point of catastrophe.** `PHI_ENCRYPTION_KEY` is one Fernet key in an env var. Lose it and **every encrypted column is permanently unrecoverable** — patient names, note contents, revisions. Leak it and all of it is exposed. Before pilot: back that key up somewhere a person can't casually delete, and write down how you'd rotate it (which, with plain Fernet, means re-encrypting every row — so know that cost now).

🧠 **Your call — Fernet-in-app, `pgcrypto`, or KMS envelope encryption?** `docs/tech-stack.md` specified `pgcrypto` and the implementation used app-layer Fernet (it works identically on SQLite, which the test suite needs). That divergence is fine but should be a decision, not an accident. Envelope encryption with a managed KMS is the answer that makes rotation tractable and keeps keys off your servers; it's also more infrastructure than a pilot may warrant. Ask Remedy's DPO what they'll require *before* wider rollout, because retrofitting is much worse than starting there.

### 4.2 Audit logging

- [x] Audit every PHI access, not just three write paths. (**7 → 23 call sites, 22 of 23 PHI-facing endpoints.** Applied as a *rule* rather than 23 separate judgment calls: log every disclosure of, or capability over, PHI; do not log requests. The one deliberate exception is `GET /upload/parts`, which returns S3 part numbers and ETags — no PHI, no capability — commented, and covered by a test named after it so the exception stays visible.)
- [x] Log the actor, action, entity, and timestamp for every read of a patient, note, transcript, or audio object. (⚠️ **`PATCH /notes/{id}` wrote no audit row at all** — the only change to clinical content that was invisible to the trail. A `NoteRevision` is not a substitute: it holds the PHI text, it dies with the note, and compliance cannot see it. `POST /patients/match` was also unlogged since Phase 0.2 — it reads like a write and is in fact a read.)
- [x] Make audit logs tamper-evident (same append-only trigger pattern as the consent ledger), **with two deliberate differences and one thing the pattern was missing.** DELETE is permitted only once `retention_expires_at` has passed, so 4.4's purge needs no superuser escape hatch; UPDATE is refused unconditionally, so that date cannot be back-dated to unlock a row. And a **statement-level TRUNCATE guard**: row triggers do not fire on TRUNCATE, so `TRUNCATE audit_logs` would have emptied the log while leaving "append-only" technically true. ⚠️ **The consent ledger still has that gap** — it is P0-1's table, was left alone deliberately, and is filed as a follow-up.
- [x] Build a review interface, or at minimum a documented query. (`GET /audit-logs` gained filters, pagination and stable ordering; **`GET /audit-logs/access-report` answers the actual question** — "who looked at this record?" — grouped by actor and action, with a LEFT OUTER JOIN so unattributed access is not silently dropped. Reading the audit log is itself audited, after the query runs. No UI: the query is the deliverable, by choice.)
- [x] Set an audit-log retention period (likely longer than PHI retention). (**2555 days vs audio's 90.** Stamped by the *column default*, so rows written outside the service still get one, and at insert time, so a later policy change cannot retroactively shorten rows already written. The reasoning is the requirement's own: "who looked at this record?" is asked *after* the record is gone, so a trail that expired with its subject could not answer the one question it is kept for.)

⚠️ **Heads-up:** "access logs" means *reads*, and reads are the ones developers forget, because nothing visibly breaks when they're missing. The failure only surfaces during an audit or a breach investigation, when the question is "who looked at this patient's record?" and the answer is "we don't know."

### 4.3 Operational security

- [x] Secrets in a real secret manager, not `.env` files on servers. — **documented, not implemented, because there is no deployment yet (Phase 5).** `docs/runbooks/secrets-management.md` names the manager, how the app loads secrets, and what changes at deploy time. Stated plainly as a design rather than shipped, so nothing here implies a control that does not exist.
- [x] Dependency scanning in CI (`pip-audit`, `npm audit`) — **and the tools were actually run, not just wired up.** `pip-audit`: **30 advisories across 6 packages, now 1.** `npm audit`: **0** — the checklist's "the mobile scaffold already reports vulnerabilities" is stale, since decision 0024 retired mobile. Exploitability in this app was near zero (no route declares `Form`/`File`; uploads go presigned direct-to-S3, so Starlette's multipart parser is never invoked; JWT decoding passes an explicit `algorithms=` allow-list) — **bumped anyway, because every one of those is a property of today's code and none of them is enforced.** The one remaining, `ecdsa` CVE-2024-23342, has no fix and never will; it is a hard dependency of `python-jose` while the app is HS256-only, so it sits in an explicit `--ignore-vuln` entry *with reasoning* rather than behind a soft-fail. The real remedy is a PyJWT migration, filed as a follow-up.
- [x] Reconsider `passlib` + the `bcrypt==4.0.1` pin — **tested rather than assumed, and the pin was wrong about its own reason.** Measured: passlib 1.7.4 works with bcrypt **4.1.1 and 4.3.0** (merely logging an `AttributeError` about `__about__`) and only breaks hard at **5.0.0**. So the project sat three minor versions behind the actual break, on the strength of a log line. Now **argon2id via `argon2-cffi`, with bcrypt 5.0.0 kept verify-only** so no existing credential is locked out, and passlib — unmaintained since 2020 — is gone, which removes the *reason* for the pin instead of carrying it forward. Credentials upgrade in place at login, since that is the one instant the plaintext exists and has just been proven correct; four tests cover the legacy hash, the upgrade, a wrong password, and a corrupt hash. Also gone: bcrypt's 72-byte truncation footgun, where a 100-character password and a wrong one sharing its first 72 bytes both verified.
- [x] Backup and *tested* restore for Postgres — **genuinely executed, not documented.** A 70 KB custom-format dump was restored into a fresh database and verified five ways: row counts identical across all 11 tables, all three `CHECK` constraints present, 12/12 patient names and 8/8 transcript blobs decrypting under the live key, and — the one that matters — the restored consent-ledger trigger **rejecting a real DELETE and a real UPDATE**, so it is enforcing rather than merely present. What was *not* tested is listed by name in the runbook: production scale, an off-host copy, dump encryption, PITR, and object-storage restore.
- [x] Breach response runbook (roadmap Week 5) — **written, never exercised, and it says so.** Grounded in this system's real data flows rather than a template: PHI in Fernet-encrypted Postgres columns, audio in object storage, transcripts and notes at Groq. ⚠️ **The legal section needs counsel**: the 72-hour NPC and data-subject notification window, the three concurrent conditions, and the DPA §30 concealment penalty were assembled from search summaries and secondary sources because `privacy.gov.ph` returned **HTTP 403** to direct fetching — NPC Circular 16-03 was *not* read first-hand, and the runbook says so rather than implying it was. **Remedy has no designated DPO and no breach response team; both are legally required and the roles table ships empty on purpose.**

### 4.4 Retention enforcement ⚠️

- [x] Implement the job that actually deletes expired audio. (`app/tasks/retention.py`, on Celery Beat hourly. Both retention columns are now read by something. The deletion stamps `audio_deleted_at` **only on a confirmed delete** — a storage failure leaves the row alone so the next sweep retries, rather than recording a deletion that did not happen.)
- [x] Extend deletion to transcripts and note revisions, not just audio. (Revisions are bulk-deleted without ever being loaded — they are `EncryptedString`, so loading them to delete them would decrypt PHI for no reason. **Signed notes and the consent ledger are never touched by any path, forced or not.**)
- [x] Log every deletion to the audit trail. (One row per artifact class, carrying the *reason* and never the object key — 0030's reasoning: an audit row outlives the retention window of what it points at. `audit.record` is called with the deletion still pending, so the deletion and its record commit together or not at all.)
- [x] Handle the withdrawal case as an immediate-deletion path. (`purge_withdrawn_encounter` **wraps** the existing `handle_withdrawal` rather than duplicating it, so P0-1's audio deletion keeps its single definition and the derived rows are added to it. The hourly sweep also treats a withdrawn encounter as due — keyed off the consent ledger rather than a 90-day clock — and routes that read through `current_consent_state`, so **re-consent after withdrawal deletes nothing**.)

🧠 **Your call — Celery Beat, a cron job, or bucket lifecycle rules?** Bucket lifecycle is the most reliable for the audio objects themselves (the storage layer enforces it whether your app is running or not) but it can't touch Postgres rows. Celery Beat keeps the logic in your app where it can cascade to transcripts and revisions. Most likely you want both: lifecycle as the backstop, an application job for the derived data. Note the open question in the PRD — the actual retention period is still owned by Legal/Compliance, so keep it configurable, which the scaffold already does.

---

## Phase 5 — Deployment and operations

### 5.1 Deployment 🧠

- [x] Choose and provision a hosting target — **chosen and specified; nothing is provisioned, and that distinction is the honest one.** Decision 0036: **one Linux VM running Docker Compose**, with managed Postgres as the single bought service and Redis self-hosted. Kubernetes rejected because the compliance bar is about *where data lives and who can read it*, and a control plane moves neither. Managed containers rejected because request-scoped containers fit Celery Beat badly. ⚠️ **The jurisdiction and provider remain Legal's**, and 0036 opens with the four ordered questions they need to answer: must PHI at rest stay physically in the Philippines; does that bind the *processor* boundary (Groq already holds the audio and transcript); does key material follow the same rule; is the provider DPA signed before the first real recording.
- [x] Production `docker-compose` or equivalent. (`infra/docker-compose.prod.yml`, with the dev-only shifted ports gone and **only 80/443 published, both from the edge container**. Verified rather than asserted: both files render; a missing required variable fails the `${VAR:?}` guard; `config --services` returns 5 without the deploy profile and 6 with it, so the migration container is genuinely unreachable from a plain `up`; `--scale beat=2` warns and starts **one**, which matters because two Beat processes double-fire the retention purge and that purge deletes PHI.)
- [x] Managed Postgres with automated backups and PITR — **specified as the one thing worth buying, and stated as unbuilt.** The reasoning is that PITR is the genuinely hard item (continuous WAL archiving plus an *exercised* restore), and a clinic day of unrecoverable signed notes is a reportable DPA availability incident rather than an outage. ⚠️ **No instance exists; PITR is a spec, not a tested path.** Phase 4.3's restore rehearsal was against local Postgres, which is a different claim.
- [x] Managed Redis, or accept and document the data-loss window — **self-hosted, and the window is documented in numbers rather than adjectives.** Redis holds only the Celery broker: no PHI, no sessions, and no rate-limit counters, because decision 0008 already put those in Postgres. Task arguments are encounter ids. So loss is roughly **one second of enqueues** under `appendfsync everysec`, and Phase 1.5's `sweep-stuck-encounters` already recovers exactly that case, bounding it at **35 minutes**. The acceptance is explicitly scoped to Redis staying on the host — moving it changes the argument.
- [x] Run migrations as an explicit deploy step, never automatically on boot. (A one-shot container behind a `deploy` profile, proven unreachable from a plain `up` by service-count diff. Nothing runs `alembic upgrade head` at import or in a lifespan hook, and the deploy script orders it explicitly.)
- [x] Health/readiness endpoints wired to the orchestrator — **and the two now mean opposite things on purpose, because they have opposite consequences.** Liveness failing restarts the container; readiness failing only removes it from traffic. So `/health` takes **no dependency at all** and `/ready` checks Postgres and Redis. Driven against the live stack by stopping containers underneath it: Redis down → `/ready` 503 `{"database":"ok","redis":"error"}` while `/health` stayed 200; Postgres down → 503 with `/health` still 200 in 0.003 s; both back → 200 **with no app restart**. Object storage is deliberately excluded — gating on it converts a partial outage into a total one, since consent, worklist, matching, review and signing all still work and P0-2 keeps recording. A leak scan of the failing body came back clean on all twelve of `remedy`, `localhost`, `5433`, `psycopg`, `Traceback`, `password` and the rest, while the same request wrote a full traceback to the log.

🧠 **Your call — where does this run, and in which jurisdiction?** Data residency is a real question for Philippine health data, and it may constrain your provider list before performance or cost does. Ask Legal early. Beyond that: a single VM with Docker Compose is the cheapest thing that works and is entirely defensible for a pilot in Remedy's own clinics; managed containers cost more and remove a class of 2 a.m. problems; Kubernetes is almost certainly wrong at this scale. Pick the least infrastructure that meets the compliance bar.

### 5.2 Observability

- [x] Structured logging with correlation IDs threaded from the **browser** request through the Celery task. (The checklist says "mobile"; decision 0024 retired that client, and the wording is corrected rather than implemented literally.) The ID is passed explicitly into the chain, because it does not propagate on its own.
- [x] Error tracking (Sentry or similar) on both API and **web**. ⚠️ **The heads-up's specific trap was real and is closed: `include_local_variables` defaults to `True` in sentry-sdk**, and a stack frame inside `generate_note` holds the entire transcript while one inside `_build_section` holds the note — so an *unrelated* `AttributeError` anywhere down that stack would have shipped the consultation to a third party. It is off at init, along with `max_request_body_size="never"`, `max_breadcrumbs=0` and `send_default_pii=False`, and a test asserts the kwargs against a fake SDK rather than trusting the docs. **It fails closed**: an SDK that rejects any safety kwarg gets no DSN at all. ⚠️ **Nobody has a Sentry account**, so today a `critical` alert is an ERROR line in a log file nothing reads.
- [x] Metrics: pipeline latency per stage, failure rate by stage, queue depth, per-consult cost. ⚠️ Stated plainly rather than implied: **emitting a number is not monitoring it**, history lives in Redis which is not durable here, and per-stage failure rate is inferred from terminal status and therefore *understates* failures.
- [x] Alerts for: pipeline failure rate, stuck encounters, queue depth, upload failure rate. (Rules run; **delivery exists only via Sentry**, which has no account yet. The monitor also runs *inside* the Beat process it watches, so it catches a wedged sweep but not a dead Beat — that needs an external check, and the runbook says so rather than leaving it to be discovered.)
- [x] A cost dashboard against the PRD's <$0.10/consult target — **and the measurement reframes the target.** Break-even is **51.7 minutes of audio**: 30 minutes costs $0.058, 60 minutes $0.116. ASR is **15–23× note generation** across the range, because Groq bills Whisper per audio-hour and `gpt-oss-120b` per token. So the PRD's cost target is really a *duration* budget, and no amount of prompt tuning moves it. ⚠️ Currently an estimate, not a measurement: real token counts need a `usage` read from the vendor responses and columns on `Note`.

⚠️ **Heads-up — scrub PHI from logs and error reports.** This is the single easiest way to leak clinical data, and it happens by accident: an exception with a transcript in the message, a request body captured by your error tracker, a debug log of a patient name. Configure Sentry's data scrubbing *before* pointing it at production, not after.

📚 **Understand first:** a correlation ID that survives the async boundary is what turns "note generation is sometimes slow" into "note generation is slow for encounters with >20 minutes of audio." Pass it explicitly into the Celery task — it will not propagate on its own.

### 5.3 CI/CD

- [x] CI: lint (`ruff`), type-check (`mypy`), test on **Postgres**, build the **web client** bundle. (Phase 4.3 built the workflow; 5.3 extended it with a `migrations` job, `REMEDY_REQUIRE_POSTGRES=1` so the Postgres suites cannot silently skip, and a `dist/` artifact upload. ⚠️ **"Build the mobile bundle" is obsolete** — decision 0024 retired the Expo client, so it is replaced by the static web bundle rather than dropped silently. ⚠️ **No job has ever run on a GitHub runner**; `actionlint` was unavailable, so action *input names* were never schema-checked and the first push is the first real run.)
- [x] Migration safety check — fail CI if a migration is missing for a model change. **Observed failing, twice**, by adding a column with no revision and watching it go red while `pytest` on the same tree stayed green — both halves of the failure side by side, then reverted byte-identically. **It ran red on first contact with the real tree and found four genuine pre-existing divergences**: two migrations created a `UniqueConstraint` *and* a unique index on the same column (`transcripts.encounter_id`, `refresh_tokens.token_hash` — a second B-tree maintained on every insert), and two enum columns were widened in Phase 0.4 without an `ALTER`. All four were harmless, which is exactly why they survived four phases: **nothing had ever compared the models against the deployed schema.** Closed by migration `b1c2d3e4f5a6`, verified reversible, and the baseline emptied — the gate now reports *"no drift: the migration chain fully expresses the models."* The gate is a **snapshot assertion, not an ignore list**: it fails when a recorded divergence *disappears* as well as when one appears, so the list shrinks toward empty instead of growing forever.
- [x] Staging environment with realistic synthetic data (never production PHI). (Seeded against live Postgres and MinIO over the real presigned-multipart path: 14 patients, 11 encounters, 8 transcripts and notes, 7 audio objects. **All 8 notes resolve citations through `resolve_grounding`**, and both of decision 0030's edit states are represented — one section edited and still fitting, another edited and no longer fitting. **Six independent locks against pointing it at production, all six observed refusing**, including the one that trusts no config at all: every `clinicians` row must be on an RFC 2606 reserved domain, so a production database refuses before anything is written.)
- [x] ~~Mobile release pipeline (EAS Build, internal distribution for pilot doctors).~~ **Obsolete — flagged, not implemented, and not silently dropped.** Decision 0024 re-platformed the client from Expo to a browser web app on a clinic laptop, and the user was explicit about not publishing to app stores. There is no EAS project and no store track. Replaced by what the web client actually needs: the `dist/` bundle uploaded per commit. Two real constraints came out of it — Vite inlines `VITE_` variables at **build** time, so the API URL is baked per-environment, and the bundle contains a service worker, so the deploy must replace the directory **atomically** or a client can mix old and new assets.

---

## Phase 6 — Pilot instrumentation

Everything the PRD promises to measure needs code before week 0, not after.

- [x] Edit-burden computation from `NoteRevision` rows — **and the 2.6 🧠 is now answered (decision 0039): a minor edit is small AND clinically inert.** A pure distance threshold is not merely imprecise, it is unsafe: `500mg` → `5000mg` is a **one-character** edit and a 10× overdose, `500mg` → `500mcg` is one character and a 1000× error with identical digits, `no chest pain` → `chest pain` is three characters and an inverted finding. Each is *maximally minor* by distance and among the most consequential corrections a doctor can make — so a metric scoring them as "the draft was basically right" would report the model as most trustworthy precisely where it was most dangerous, **in the very number being used to justify skipping vendor validation.** The clinical check is therefore a **veto, not a weighting**: no similarity score can make a changed dose minor. It covers quantities, dose units and negations **including Filipino ones** (`wala`, `hindi`, `walang`), because P0-3 keeps Taglish verbatim and a negation flip in the patient's own words is the same inversion.
- [x] Correctly-filed-rate tracking — **reported as a *caught-error* rate, because that is what it honestly is.** The system observes filings it *rejected* (P0-6's 409s, when a confirmed patient does not match the encounter). It cannot observe a note filed to the wrong patient that the doctor confirmed anyway, because at that point every check agrees. Presenting it as the true correctly-filed rate would be the most flattering possible reading of the data; the real figure needs the weekly manual review below, and the schema says so rather than implying otherwise.
- [x] Post-encounter five-star rating prompt. (Appears **after signing, never before** — asking mid-workflow buys a rating at the cost of interrupting the clinical task. Dismissible, one click to submit, comment optional. This is the only pilot signal nothing can derive, so response rate is the thing to protect: a prompt that must be answered trains people to click a star at random, which is **worse than no data because it looks like data**. The comment box is the one field here a doctor could type a patient's name into, so it carries a warning, is encrypted at rest, and is excluded from the report.)
- [x] Documentation-time measurement, comparable to the week-0 paper baseline — **split in two, because one number would be unusable.** Encounter-creation-to-signature is what compares to the paper baseline; generation-to-signature is the part the product actually controls. A doctor who leaves a note open over lunch inflates the first and not the second, and conflating them would make the headline figure meaningless.
- [x] Voluntary-use tracking. (Counts **distinct ISO weeks with at least one encounter**, not volume — "still using it in week 4" is a question about spread, and one 40-encounter day followed by silence is exactly the pattern a total would hide. Counts encounters *created* rather than notes signed, because the question is whether the doctor reached for it at all: a recording started and abandoned still answers it.)
- [x] A weekly manual-review sampling workflow for unsafe-acceptance rate. (**Deterministic** — the same week returns the same ids, so two reviewers read the same notes and a reviewer can stop and resume; `random.sample` would give each reviewer a different set and make disagreement uninterpretable. **Safety-flagged notes come first**, because the point is catching an unsafe acceptance, not estimating a mean over a mostly-fine population. Returns **ids only**: an endpoint that returned note text would be an unaudited bulk PHI export wearing a different hat, so the reviewer opens each note through the normal route and the read is audited like any other. ⚠️ The reviewer's *verdict* is not captured anywhere yet, so the rate itself is still computed off-system.)

⚠️ **Heads-up:** the roadmap's stated mitigation for skipping the vendor bake-off is "watch the edit-burden metric closely from day one of internal alpha." That mitigation only exists if the metric is instrumented *before* alpha. If it isn't, the accepted risk quietly becomes an unmonitored risk — the worst outcome available, because you'll have neither pre-validation nor early detection.

---

## Phase 7 — P1 fast-follows (post go/no-go)

Build only after the Week 6 checkpoint passes.

- [ ] Patient-facing plain-language summary (Filipino, 6th-grade reading level, on request, delivered before the patient leaves).
- [ ] Referral letter drafting.
- [ ] Prior-visit Assessment/Plan auto-injected into the note-generation prompt as labeled historical context.
- [ ] Front-desk check-in queue integration (also the cleanest answer to the 2.5 patient-search problem).
- [ ] Dermatology quick-entry pad for Objective findings.

📚 **Understand first:** the PRD's P2 list is a set of things not to architect *out*. When you make choices in Phases 1–5, occasionally sanity-check them against per-doctor templates, medical certificates, multi-tenancy, and EMR integration. You don't build for them — you just avoid decisions that would make them require a rewrite. The `NoteGenerator` interface is a good example of this done right: swapping models is a config flag because someone thought about it in advance.

---

## Cross-cutting: what to write down as you go

Create `docs/decisions/` and record every 🧠 above as a short entry: the decision, the options considered, why you chose, and what would change your mind. Four sentences is enough.

Two reasons this matters more than it seems. First, in eight weeks you will not remember why you picked chunked-upload-by-hand over presigned URLs, and neither will anyone reviewing it. Second — and this is the part that's specific to you — writing the "what would change my mind" line is what turns a decision you *made* into a decision you *understand*. If you can't fill that line in, you probably haven't finished thinking about it yet.

Keep `docs/tech-stack.md` honest as reality diverges from it. It already has one divergence worth fixing: it specifies `pgcrypto` and the code uses app-layer Fernet. A stack doc that quietly stops matching the code is worse than no stack doc, because people trust it.

---

## Suggested sequencing

Given a lean team and the roadmap's 4–8 week MVP target:

| Order | Work | Why here |
|---|---|---|
| 1 | Phase 0 | These are false claims in the codebase. Fix before building on them. |
| 2 | Phase 1.1–1.2 | Upload + transcript persistence unblock everything downstream. |
| 3 | Phase 2.1–2.2 | Recording is the longest-lead, highest-risk work. Start early, fail early. |
| 4 | Phase 1.3–1.5 | Real ASR + note-gen, now that you have real audio to feed them — plus the failure handling that makes them safe to run unattended. |
| 5 | Phase 2.3–2.6 | Consent, queue, identity, review/sign → **internal alpha**. |
| 6 | Phase 6 | Instrument *before* alpha, not after. |
| 7 | Phase 3 | Grounding UI. |
| 8 | Phase 4–5 | Security hardening + deployment before real patients. |
| 9 | → Go/no-go | Then Phase 7. |

Phase 4 sits late in this list but has a hard constraint: **no real patient data touches this system until 4.1, 4.2, and 4.4 are done**, regardless of what else is finished. Alpha on synthetic or fully-consented internal recordings is fine before then. Real consultations are not.

And the standing blocker from the roadmap: consent-gated recording cannot go live to real patients until Philippine counsel clears the RA 4200 flow. Build it, test it, keep it dark until Legal signs off.
