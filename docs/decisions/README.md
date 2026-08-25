# Decisions log

One file per decision, in the format `docs/implementation-checklist.md` asks
for: the decision, the options considered, why we chose what we chose, and
what would change our mind. Four sentences is enough — this is a log, not a
design doc.

Every 🧠 **Your call** in the checklist gets an entry here. Some entries are
mine (an implementation-level choice made while executing a checklist item —
still worth a record so we don't re-litigate it or forget the reasoning);
those are marked **Decided by:** implementation. Anything that's actually the
product/legal/risk fork the checklist flags for you is marked **STATUS: OPEN
— your call** and left unfilled until you decide it.

| # | Phase | Decision | Status |
|---|---|---|---|
| [0001](0001-consent-ledger-read-model.md) | 0.1 | How `assert_consent_valid` reads "current" consent state from an append-only ledger | Decided (implementation) |
| [0002](0002-consent-violation-is-terminal-not-retried.md) | 0.1 | What the pipeline does when consent is invalid at task time | Decided (implementation) |
| [0003](0003-mid-visit-reconsent-detection.md) | 0.1 / 2.3 | Manual flag vs. diarization-based detection for mid-visit re-consent | **OPEN — your call** |
