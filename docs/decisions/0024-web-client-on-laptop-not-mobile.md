# 0024 — Client is a browser-based web app on a clinic laptop, not a mobile app

**Phase:** 2.1 · **Decided by:** user (form factor, on the supervisor's answer) + measurement (viability) · **Date:** 2026-08-27

**Decision:** the doctor-facing client is a **browser-based web app (PWA-shaped:
service worker offline shell + IndexedDB write-ahead queue), running in
Chrome or Edge on a clinic laptop.** `apps/mobile/`'s Expo scaffold is
retired. This reverses `docs/tech-stack.md` §1 ("cross-platform mobile, not
a web/PWA client") — and the reversal is legitimate rather than a
regression, because tech-stack.md's stated reason was specific to a form
factor that turned out not to be the real one.

## Why the original reasoning no longer applies

tech-stack.md §1 rejected web on exactly two grounds, both phone-specific:

1. *"**mobile browsers** suspend microphone capture on background/lock."*
   True, and still true — but irrelevant on a laptop. Desktop Chrome
   exempts a tab with active `getUserMedia` capture (and an AudioContext
   connected to `destination`) from background timer throttling and tab
   freezing. Measured below.
2. *"A phone app is also the framing throughout the PRD's user stories."*
   That framing rested on an assumption, not a requirement. The PRD listed
   *"What devices do doctors actually carry, and does a phone fit the
   physical workflow?"* as an Open Question owned by Design/Engineering,
   explicitly non-blocking, to be answered in week-0 shadowing. **The
   supervisor answered it on 2026-08-27: these run primarily on laptops.**
   This decision is that open question closing, not an override of it.

Note also what falls away with it: tech-stack.md's Expo-custom-dev-client
choice existed *solely* to enable background-audio native modules
(Android foreground service, iOS `AVAudioSession` background mode). With
no phone, that entire branch of complexity — and the "genuine time sink"
toolchain warning attached to it in checklist 2.1 — disappears rather
than being worked around.

## The measurement that settled it

`docs/experiments/audio-capture-harness.html` (with its own Playwright
self-test, `verify-harness.js`). One 29-minute run on the real target
hardware — Windows, Chrome 151, Intel Smart Sound mic:

| Measure | Result |
|---|---|
| Run length | 1744 s (29.1 min) — a real consultation length |
| Audio missing, total | **7.69 s** |
| ...attributable to one system-sleep event at 122.8 s | **6.51 s** |
| ...spread across the other 345 chunks | 0.62 s (1.8 ms/chunk — Opus VBR framing noise) |
| Backgrounded windows | 9, longest 56.7 s, 131 s of hidden time total |
| **Audio missing during backgrounding** | **0.05 s** |
| Worst chunk interval excluding the sleep | 5.05 s against a requested 5.00 s |
| Page timer ticks | 1737 / 1743 |
| Worker timer ticks | 1737 / 1743 (identical; the 6 missed match the sleep) |

Two independent methods agree: the worklet sample clock reports 7.69 s
missing, and per-chunk byte accounting reports 7.13 s — both attributing
~6.5 s to the single sleep. `maxWorkletMsgGapMs` (6.55 s) independently
corroborates the same event.

**The conclusion is unusually clean: every measurable loss came from one
system suspend. Backgrounding, blur, and screen visibility changes cost
50 milliseconds across 131 seconds of being hidden — zero, within noise.**

Two consequences worth stating explicitly:

- **Page timers did not throttle at all** (1737 vs 1737, identical to the
  worker). So the Worker-based encoding path I had flagged as probably
  necessary is **not** needed here. The mechanism is Chrome's exemption
  for tabs holding active capture with a live audio graph; a production
  client will have both, so the exemption holds. If a future
  implementation ever drops the AudioContext and uses `MediaRecorder`
  alone, re-run the harness before trusting it.
- **Lid close / sleep is the one real risk, and it does not favour
  Electron.** Lid close is OS power policy; neither a web page nor
  Electron's `powerSaveBlocker` can veto it (`powerSaveBlocker` blocks
  *idle* sleep only). On Windows it is mitigable as device config:
  Power Options → "When I close the lid" → Do nothing, pushed to clinic
  laptops. That is an IT setting, not an architecture choice.

## Options considered

- **(a) Browser web app on laptop, as chosen.** Zero install, zero
  distribution pipeline, instant updates across every clinic laptop, no
  store, no Mac, no signing. Reuses the existing FastAPI backend
  unchanged — including the presigned S3 multipart endpoints from Phase
  1.1, which the browser `PUT`s to directly.
- **(b) Electron desktop app.** Its one genuine remaining advantage is a
  hardware-sealed encryption key (`safeStorage` → DPAPI/Keychain) versus
  a browser's non-extractable `CryptoKey` in IndexedDB. That is a
  compliance conversation, not a functional blocker, and does not justify
  a packaging/signing/update pipeline for a 4–8 week MVP. Kept as the
  documented fallback if the measurement had failed — it did not.
- **(c) Keep the Expo mobile app.** Would have satisfied P0-2's
  interruption clause most completely, but pays the full toolchain cost
  (Android Studio; Mac + paid Apple account for iOS) to serve a form
  factor the supervisor says is not the one in use.
- **(d) Dedicated hardware recorder** (Plaud-style). **Disqualified on
  legal grounds, not cost.** P0-1 requires the app to *block* recording
  until consent is logged and the script presented. A dumb recorder
  cannot enforce a consent gate, so it fails the legal basis for
  recording at all.

## What P0-2 now means

P0-2's interruption clause — *"Recording survives interruption (incoming
call, app backgrounded, device locked) without data loss"* — re-scopes to
the laptop form factor rather than being descoped:

- *incoming call* — essentially N/A on a clinic laptop.
- *app backgrounded* — **measured: satisfied.** 0.05 s over 131 s hidden.
- *device locked* — **measured: satisfied** in this run.
- *lid close / system sleep* — **not satisfied, and unsatisfiable in
  software.** Mitigated by Windows power policy plus chunked IndexedDB
  writes so a suspend truncates rather than destroys the recording. This
  needs stating plainly to whoever signs off on P0-2; the run above
  produced 6.5 s of real loss from one lid close.

## Two harness bugs this run exposed, both fixed

1. **The screen wake lock was never re-acquired.** Acquired at 0.0 s,
   auto-released by the browser at 35.1 s on the first backgrounding, and
   never restored for the remaining 28 minutes — so idle sleep was
   unguarded for 97% of the session, and a suspend duly followed at
   122.8 s. Wake Lock auto-releases on hide and does not self-restore;
   re-acquiring on every `visibilitychange → visible` is mandatory. **The
   production client needs this same logic.**
2. **The loss threshold was a percentage.** 0.441 % drift passed a
   `< 0.5 % = fine` gate while representing 7.69 seconds of missing
   consultation audio — plausibly a whole exchange about a dose. Only the
   independent sleep detector caught it. Thresholds are now on absolute
   seconds (ok < 1 s, bad ≥ 5 s) with the percentage demoted to context.

## What would change my mind

- If clinic laptops turn out to be MacBooks after all: clamshell sleep is
  materially harder to defeat than Windows lid policy (it needs power
  *plus* an external display to stay awake), and Safari should be avoided
  in favour of Chrome/Edge regardless. Re-run the harness on the actual
  machine before relying on this decision there.
- If doctors turn out to record while walking between rooms rather than at
  a desk, the laptop premise fails and this whole decision reopens — that
  is the same Open Question, asked again.
- If Legal requires hardware-sealed key custody for on-device PHI, option
  (b) Electron becomes necessary and this becomes a wrapper decision
  rather than a rewrite.
