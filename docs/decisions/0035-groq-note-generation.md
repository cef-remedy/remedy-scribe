# 0035 — Note generation moves to Groq; the free tier is not the reason it's right

**Phase:** 4 (vendor) · **Decided by:** user (vendor), implementation (model, mechanism, fallback) · **Date:** 2026-08-28

## The ask, and the part of it that doesn't survive checking

> "Use a Groq AI model for the note generation as well instead of Haiku because they offer free permanent tiers."

The vendor change is right. **The stated reason is not, at this system's scale**, and acting on it without saying so would set up a failure during the pilot rather than now.

Groq's free tier allows roughly **8,000 tokens per minute** on the candidate models. A 20–40 minute consultation transcript is comfortably **10,000–20,000 tokens**, and note generation sends the whole thing in one fused call. A single real consultation therefore exceeds the per-minute allowance outright. Short visits will work; a clinic day will not, and the failure will look like intermittent note-generation errors rather than an obvious quota wall.

Second, and more consequential for a system handling PHI under the Data Privacy Act: Groq's Business Associate Addendum covers "Covered Cloud Services", and that definition **explicitly excludes services "provided for free or at no additional charge"**, along with anything in beta/preview/trial status. So the free tier is exactly the tier that carries no data-protection undertaking. For a clinical product that is not a cost trade-off; it is a disqualifier.

**Conclusion: make the change, on a paid plan.** The recommendation is not "don't do this" — it's "do this, and budget for the Developer plan."

## The reason the change is right anyway

Phase 1.3 already sends every consultation's **audio** to Groq for transcription (decision 0018). Groq therefore already holds the most sensitive artifact in the system — the verbatim recording — and derives the transcript from it.

Generating the note there too discloses **nothing new**. It does, however, collapse two processors into one:

- one vendor to contract with, and one DPA rather than two;
- one entry in the Data Privacy Act processor disclosure, and one set of cross-border transfer terms;
- one vendor to name in the breach-response runbook (Phase 4.3);
- one dependency whose outage is a single, legible failure mode instead of two partially-overlapping ones.

Adding a *second* AI vendor to a healthcare product buys real compliance overhead. Removing one is worth more than the token pricing either way.

What is verified and genuinely good: Groq's Services Agreement states that **"Groq is not permitted to use Inputs or Outputs for training or fine-tuning any AI Model Services or other models"**, that it "does not access, use, store, or retain Inputs or Outputs except as necessary to provide the Cloud Services", and that by default it "does not retain customer data for inference requests", with Zero Data Retention available in Data Controls. That is a stronger written position than the absence of one, and it applies to the transcript we already send.

## Model: `openai/gpt-oss-120b`, and status is the deciding factor

Candidates on Groq that support strict structured output: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`.

`qwen/qwen3.8-27b` is the tempting one — same strict-schema support, and a **2,000,000** token/day allowance against gpt-oss-120b's 200,000. It is rejected anyway, because it is **Preview status**, and preview models sit inside the BAA's exclusion list alongside free services. A model that cannot lawfully carry PHI is not a candidate no matter how generous its quota.

That leaves the two gpt-oss models, both Production, both 131,072-token context — far more than a consultation needs. `120b` is the default for reasoning quality; `20b` is the same schema support and roughly twice the speed, so it degrades gracefully if throughput ever matters more than nuance. Both are configurable via `GROQ_NOTE_MODEL`, for the same reason `GROQ_WHISPER_MODEL` is: Groq's catalogue moves, and models carry published shutdown dates. (Note `llama-3.3-70b-versatile` and `llama-3.1-8b-instant`, the obvious first guesses, are already **past their 2026-08-16 shutdown date** and are not free-tier models.)

## Mechanism: forced tool use → strict structured outputs

This is the part of the port that is not a model-ID swap.

The Anthropic path pins the response shape with `tool_choice`. Groq's OpenAI-compatible API supports that same shape — but its documentation is explicit: **"Streaming and tool use are not currently supported with Structured Outputs."** The two mechanisms are mutually exclusive, so the naive port (send `tools` *and* `response_format`) does not work.

Given a forced choice, `response_format: {"type": "json_schema", strict: true}` is the **stronger** of the two. Strict mode is constrained decoding: the model cannot emit a token sequence that violates the schema. A forced tool call still asks a model to fill a schema correctly and hopes. For the one output in this system that the entire grounding feature depends on, an enforced schema is a real improvement, not a workaround.

The costs, both handled:

- **Arguments arrive as a JSON string**, not a parsed object (`choices[0].message.content`), so `json.loads` is now a failure path that did not previously exist. A response truncated at `max_tokens` returns HTTP 200 carrying invalid JSON — named explicitly as `GroqNoteParseError` rather than left to surface as a mysterious 500.
- **Strict mode rejects schemas** that omit `additionalProperties: false` or leave any property out of `required`. That rejection only happens against a live key, which is not available in this environment, so a test asserts the schema's shape recursively instead.

## What is shared, and why that is a correctness requirement

The prompt, the schema, the mechanical `[INAUDIBLE]` substitution, the server-side offset assignment and the citation verification now live in `note_generation/shared.py`, used identically by both providers. This is **not** a DRY tidy-up.

`build_section` joins sentences with a single space and records each sentence's offsets as it goes. Phase 3's `grounding.spans_fit_text` re-derives exactly that invariant — it slices a note's text by its stored spans, re-joins with a single space, and expects the original text back. A provider that assembled sections even slightly differently (a newline separator, say) would generate notes whose grounding **never lines up, from birth**: every section would render "source links no longer line up with the text", on one vendor only, with nothing raising and every test passing.

That is a Phase 4 vendor swap silently breaking a Phase 3 feature. A test now asserts the invariant end-to-end from generated output, and a second asserts both providers build sections through the identical function object.

## Haiku is kept, which contradicts two earlier decisions on purpose

Decision 0021 deleted `LunaNoteGenerator` and 0018 deleted the ElevenLabs stub, both on the principle that *"an unused alternative with no real distinguishing capability left is dead weight, not a safety net."*

That clause is a test, and Haiku now passes it. It has two distinguishing capabilities Groq currently lacks: no 8,000 TPM ceiling, and a commercial agreement that covers paid use without the free-tier carve-out. Until Groq is on a paid plan with a signed DPA, `NOTE_GENERATOR_PROVIDER=haiku` is one environment variable between a blocked pilot and a working one. That is the definition of a safety net rather than dead weight.

It is demoted, not retained by default: Groq is the default, and the swap point in `get_note_generator()` is now genuinely exercised by two options instead of one.

## A pre-existing PHI leak found while porting

`_extract_tool_input` raised `RuntimeError(f"...: {response_json!r}")` — interpolating the **entire** Anthropic response into the exception message.

`app/tasks/pipeline.py:_mark_stage_failure` writes `str(exc)[:500]` into `Encounter.last_pipeline_error`, which is a plain **unencrypted** `String(500)` column. That column's own comment argues it is safe precisely because "every exception raised from these two tasks is an infrastructure/vendor error ... never something built from transcript or note content, so this can never leak PHI."

On this path that claim was false. A response that omitted the tool block — the exact case the error handles — typically contains the model's prose *about the consultation*, so generated clinical content would have been written to an unencrypted column, in a database, indefinitely.

Both providers now report the **structure** of a bad response (stop/finish reason, block types, parse offset) and never its content. Two tests assert that a planted patient name cannot reach the exception message. This is the kind of leak that is invisible until an audit, and it was found by porting rather than by any test.

## What would change my mind

- **Groq publishes free-tier limits that fit a consultation, or a paid plan is approved.** The first removes the need to keep Haiku; the second removes the blocker outright and is the expected path.
- **Taglish quality turns out worse than Haiku's.** This is the single unverified risk in the swap: Groq publishes no multilingual benchmarks for gpt-oss, and P0-3 requires Filipino preserved verbatim while P0-4 requires hedged clinical language. Neither is a given on a model chosen from a capability table. **A head-to-head on real Taglish transcripts should run before the pilot, not after** — `prompt_version` is stored per note precisely so the comparison is reconstructable from the data.
- **Legal or the DPO objects to a single processor holding audio, transcript and note.** Consolidation is an argument about contract surface, not about isolation. A reviewer who wants the note generated by a different vendor than the one holding the audio has a coherent position, and it is theirs to take.
