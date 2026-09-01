# Remedy Scribe

In-clinic AI consultation note-taker for a Philippine health clinic: records
a consultation with consent, transcribes it, drafts a clinical note, and lets
the doctor check every line against the recording before signing it.

**→ To run it: [`docs/runbooks/local-development.md`](docs/runbooks/local-development.md).**
That is the one document to start from, for you or for a new developer.

- `remedy-scribe-prd.md` — requirements (P0-1 … P0-8)
- `remedy-scribe-roadmap.md` — sequencing
- `docs/implementation-checklist.md` — what exists, what is deliberately absent, and why
- `docs/decisions/README.md` — every non-obvious choice, with its reasoning
- `docs/progress/README.md` — what each phase built, and the bugs it caught

## Layout

```
apps/
  api/      FastAPI backend — patients, encounters, consent ledger, note
            lifecycle, ASR + note-generation pipeline, grounding, retention,
            audit log, pilot metrics
  web/      Vite + React browser client — the doctor's app, on a clinic
            laptop (decision 0024; there is no mobile app)
infra/
  docker-compose.yml        postgres, redis, minio — local dev
  docker-compose.prod.yml   the production topology (specified, not deployed)
docs/
  runbooks/    how to run, deploy, rotate keys, restore, respond to a breach
  decisions/   0001-0039
  progress/    one file per phase
```

## Status

Phases 0 through 6 are complete: **427 API tests** (408 on SQLite alone, 19
more needing the Postgres/MinIO containers), **61 web unit tests**, **145
end-to-end browser checks**, `ruff`/`mypy`/`tsc` clean.

What remains is mostly not engineering — a paid Groq plan, a Sentry account,
a managed Postgres and a VM, plus Legal clearing the RA 4200 consent script
and Remedy designating a DPO. The checklist's "honest headline" section is
kept current and is the authoritative answer.

⚠️ **Do not record a real patient yet.** The consent mechanism is complete
and tested, but the script it reads out is a placeholder pending counsel.
Recording without valid consent is a criminal matter under the Philippine
Anti-Wiretapping Act (RA 4200), not a product bug. Testing with your own
voice is fine.

## The parts most worth understanding

**Grounding refuses to guess.** Tap a note line and it shows the transcript
passage it came from. If a doctor's edit has shifted the stored character
offsets, it says *"source links no longer line up"* rather than highlighting
approximately — for a feature whose only job is proof, a confidently wrong
answer is worse than no answer (decision 0030).

**"Minor edit" is small *and* clinically inert.** The PRD's headline target
is "≥70% of signed notes require only minor edits", and a plain distance
threshold would call `500mg` → `5000mg` a one-character edit. It is a
tenfold overdose. The clinical check is a veto, not a weighting
(decision 0039).

**Audio availability is verified, never assumed.** The object store's own
lifecycle rule deletes recordings with nothing updating the database, so the
row claiming a recording exists is not evidence that it does.

**PHI stays out of logs mechanically.** Log records are assembled from an
allow-list at a boundary no call site can route around, rather than scrubbed
by a denylist someone has to remember to update (decision 0037).
