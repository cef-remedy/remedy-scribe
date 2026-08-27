# 0029 — Searchable encrypted patient names: the checklist's own option doesn't fit

**Phase:** 2.5 · **Decided by:** implementation, after measuring · **Date:** 2026-08-27

## The 🧠 as posed, and why its preferred option is wrong here

The checklist asks how to get searchable encrypted names and offers:

1. **Blind index** — store an HMAC of the normalized name and search that.
   "Enables exact and prefix matching while the name stays encrypted.
   Standard solution."
2. **Don't encrypt the name**, rely on DB/disk encryption instead.
3. **Keep birthdate-first matching** and design the UX around it.

Option 1 solves the wrong half of the problem. Re-reading P0-6:

> "Starting a session accepts a typed or dictated patient name and
> **fuzzy-matches** against the existing directory"

An HMAC supports **equality only**. You cannot compute a similarity ratio
against a hash, so a blind index gives exact match and nothing else — and a
doctor typing "Maria Cruz" for *Maria Santos Dela Cruz*, or fat-fingering
"Cruzz", gets nothing. Prefix matching is also not really available: HMAC of
a prefix ≠ prefix of an HMAC, so you would need a separate index per prefix
length.

Option 2 makes every Postgres read a PHI read, in a system under the Data
Privacy Act, to solve a problem that turns out not to need it.

Option 3 contradicts the requirement's own entry point — birthdate-first
means the doctor must know a birthdate before they can look anyone up.

## Decision: decrypt and compare in Python — but implemented against measurements

Names are decrypted server-side and ranked with `difflib`. No new key, no
schema change, encryption untouched, and true fuzzy matching.

The naive version of this is far too slow, which is why it was measured
rather than assumed. At 5,000 patients, `db.query(Patient).all()` plus
similarity over every row took **~2.1 seconds** — unusable. The component
breakdown is what redirected the fix:

| step | cost at N=5,000 |
|---|---|
| raw `SELECT` of ciphertext | 7.7 ms |
| + decrypting every value | 118 ms |
| full ORM query instead of raw SELECT | **348 ms** |
| `difflib` over all names | 183 ms |
| token prefilter, then `difflib` on survivors | **68 ms** |

**Decryption is not the bottleneck.** ORM hydration and unfiltered `difflib`
are. So the implementation:

- issues a **raw `SELECT` of the three columns needed** (id, name, birthdate)
  rather than materialising ORM objects that are immediately discarded;
- **prefilters on a shared token or prefix** before any similarity work — a
  candidate sharing no token and no 3-character prefix with the query is not
  a plausible typo, so skipping it costs no meaningful recall;
- keeps a shared token as evidence in its own right, so a partial-name query
  survives a low whole-string ratio.

Combined: ~194 ms at 5,000 patients, roughly 10× the naive path.

## The scale ceiling, stated as a number

This is O(N) in directory size. Measured p50 for the naive path was 172 ms
at 500 patients, 634 ms at 2,000, 2.1 s at 5,000, 7.7 s at 20,000; the
optimized path is roughly an order of magnitude better at each point but
still linear.

That is acceptable because the PRD scopes v1 to a single clinic's directory
— multi-tenancy is P2, and "Remedy operates all its own clinics". Search is
also debounced and submit-shaped rather than per-keystroke, so one lookup is
one query.

**If the directory outgrows this**, the right next step is a **token-level
blind index**: an HMAC per name token, so SQL performs the prefilter and
Python only ranks the survivors. Note this is strictly better than the
whole-name blind index the checklist proposed, because it preserves fuzzy
matching instead of replacing it with equality. It costs a second key and
leaks token equality (you learn two patients share a surname without
decrypting), which is why it is not being added before it is needed.

## Search does not become a deduplication path

Kept deliberately separate, because collapsing them would break P0-6's
"dedup uses name + birthdate together, not name alone":

- **`search_patients_by_name(name)`** answers *who might the doctor mean?*
  Takes no birthdate. Returns candidates **with** their birthdates, because
  that is what distinguishes two similar names.
- **`match_patient(name, birthdate)`** answers *is this the same person?*
  Requires both. Unchanged from Phase 0.

A test asserts the split directly: same name + wrong birthdate is a search
hit but **not** a dedup match.

## Two related choices

**"Links silently" applies only to a single exact match.** P0-6 says an
exact match links without confirmation. The client applies that only when
there is exactly *one* exact hit — two patients with an identical name is
precisely the case where silence attaches the note to the wrong person, so it
falls through to confirmation.

**Search is audited as a PHI read.** It decrypts every name in the directory
to rank them. The query string itself is deliberately *not* recorded: a
patient name in an audit log is PHI in a table with longer retention than the
record it describes (Phase 4.2's own heads-up).

## What would change my mind

- A directory beyond ~10,000 patients, or a move to a shared multi-clinic
  directory: switch to the token-level blind index above.
- If Legal ever requires that patient names not be decrypted in bulk even
  server-side — a defensible position, since this reads the whole directory
  per search — then the token blind index becomes mandatory rather than an
  optimization, because it is the only option that narrows before decrypting.
