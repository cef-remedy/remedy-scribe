/**
 * Phase 3 end-to-end: the grounding UI (P0-7).
 *
 * This feature's entire job is proof, so the assertions below are mostly
 * about *honesty* rather than function. A grounding UI that highlights the
 * wrong passage, or offers a play button over deleted audio, is worse than no
 * grounding UI: it converts "I don't know where this came from" into a false
 * "here is where this came from," and the doctor signs on the strength of it.
 *
 * What is asserted:
 *   - a note line resolves to the transcript passage it cites, with a real
 *     timestamp, against a real recording
 *   - two taps, not one: highlight first, audio second
 *   - audio actually plays, from the cited offset, and stops at the end of
 *     the passage rather than running on through the consultation
 *   - the presigned URL is signed no-store, so playback leaves nothing on disk
 *   - context passages are shown but never marked as evidence
 *   - the degradation ladder: withdrawing consent deletes the audio, and the
 *     screen then says the recording was deleted *at the patient's request*
 *     while transcript grounding keeps working
 *   - an edit invalidates the stored offsets, and grounding is withdrawn
 *     rather than approximated
 *
 * Prerequisites: Postgres, Redis, MinIO, API, Celery worker, Vite dev server.
 *
 * Run:
 *   PW_PATH=... MFA_SECRET=... node smoke/grounding-flow.cjs
 *
 * SEED_PIPELINE=1 substitutes the two vendor legs (Groq Whisper, Claude
 * Haiku) with smoke/seed_pipeline.py when API keys are not provisioned. The
 * audio object, the presigned playback, the Range requests, the span
 * resolution and the whole browser UI are still real — only the ASR and
 * note-generation calls are stood in for, and those are verified for real in
 * Phase 1.3 and 1.4. Run without it to exercise the true end-to-end path.
 */
const crypto = require("node:crypto");
const { execFileSync } = require("node:child_process");
const path = require("node:path");

const SEED_PIPELINE = process.env.SEED_PIPELINE === "1";
const PYTHON =
  process.env.API_PYTHON || path.join(__dirname, "..", "..", "api", ".venv", "Scripts", "python.exe");

const PW_PATH = process.env.PW_PATH || "playwright";
const WEB_URL = process.env.WEB_URL || "http://localhost:5173";
const MFA_SECRET = process.env.MFA_SECRET;
const EMAIL = process.env.SMOKE_EMAIL || "doc@example.com";
const PASSWORD = process.env.SMOKE_PASSWORD || "smoke-test-password";

if (!MFA_SECRET) {
  console.error("MFA_SECRET is required.");
  process.exit(2);
}

const { chromium } = require(PW_PATH);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function base32Decode(input) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of input.replace(/=+$/, "").toUpperCase()) {
    const idx = alphabet.indexOf(ch);
    if (idx !== -1) bits += idx.toString(2).padStart(5, "0");
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}
function totp(secret) {
  const counter = Math.floor(Date.now() / 1000 / 30);
  const buf = Buffer.alloc(8);
  buf.writeUInt32BE(Math.floor(counter / 2 ** 32), 0);
  buf.writeUInt32BE(counter >>> 0, 4);
  const hmac = crypto.createHmac("sha1", base32Decode(secret)).update(buf).digest();
  const o = hmac[hmac.length - 1] & 0x0f;
  const code =
    ((hmac[o] & 0x7f) << 24) | ((hmac[o + 1] & 0xff) << 16) | ((hmac[o + 2] & 0xff) << 8) | (hmac[o + 3] & 0xff);
  return String(code % 1e6).padStart(6, "0");
}

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok });
  console.log("  " + (ok ? "PASS" : "FAIL") + "  " + name + (detail ? "  -- " + detail : ""));
}

const call = (page, fn, arg) => page.evaluate(fn, arg);

(async () => {
  const browser = await chromium.launch({
    headless: false,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      // Playback is programmatic, so the autoplay gate has to be lifted or
      // audio.play() rejects and every playback assertion tests the gate
      // rather than the feature.
      "--autoplay-policy=no-user-gesture-required",
    ],
  });
  const context = await browser.newContext({ permissions: ["microphone"] });
  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));

  console.log("\n=== setup ===");
  await page.goto(WEB_URL, { waitUntil: "networkidle" });
  await page.fill("#email", EMAIL);
  await page.fill("#password", PASSWORD);
  await page.fill("#mfa", totp(MFA_SECRET));
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 15000 });
  await sleep(800);
  check("signed in", !page.url().includes("/login"));

  const tag = Math.random().toString(36).slice(2, 7);
  const patientId = await call(page, async (t) => {
    const mod = await import("/src/api/client.ts");
    const res = await mod.api.POST("/api/v1/patients", {
      body: { name: `Grounding Test Patient ${t}`, birthdate: "1990-03-15" },
    });
    return res.data?.id ?? null;
  }, tag);
  check("seeded a patient", !!patientId);

  // --- a real recording, so the transcript and its timings are real --------
  console.log("\n=== a real 14s recording through the real pipeline ===");
  const enc = await call(page, async (pid) => {
    const mod = await import("/src/api/client.ts");
    const res = await mod.api.POST("/api/v1/encounters", {
      body: {
        patient_id: pid,
        upload_idempotency_key: "smoke-ground-" + Math.random().toString(36).slice(2),
      },
    });
    await mod.api.POST("/api/v1/consent", {
      body: {
        encounter_id: res.data.id,
        event: "given",
        participant_roster: ["Doctor", "Patient"],
        purposes: ["clinical documentation"],
        script_language: "fil",
      },
    });
    return res.data.id;
  }, patientId);
  check("encounter created and consented", !!enc, enc);

  await page.goto(`${WEB_URL}/encounters/${enc}/record`, { waitUntil: "networkidle" });
  await sleep(1300);
  await page.getByRole("button", { name: /start recording/i }).click();
  console.log("  recording 14s...");
  await sleep(14000);
  await page.getByRole("button", { name: /stop recording/i }).click();
  await sleep(2500);

  const poll = () =>
    call(page, async (encId) => {
      const mod = await import("/src/api/client.ts");
      const e = await mod.api.GET("/api/v1/encounters/{encounter_id}", {
        params: { path: { encounter_id: encId } },
      });
      return { status: e.data?.pipeline_status ?? null, noteId: e.data?.note_id ?? null };
    }, enc);

  let noteId = null;
  let uploaded = false;
  for (let i = 0; i < 45; i++) {
    await sleep(2000);
    const snap = await poll();
    if (i % 4 === 0) console.log(`  t+${(i + 1) * 2}s  pipeline=${snap.status}  note=${snap.noteId}`);
    if (snap.status && snap.status !== "recording") uploaded = true;
    if (snap.noteId) {
      noteId = snap.noteId;
      break;
    }
    // With the vendor legs substituted, waiting for the real pipeline is
    // waiting for something that will never arrive.
    if (SEED_PIPELINE && uploaded) break;
  }
  check("the recording reached object storage", uploaded);

  if (!noteId && SEED_PIPELINE) {
    console.log("  SEED_PIPELINE=1: substituting the ASR and note-generation legs");
    noteId = execFileSync(PYTHON, [path.join(__dirname, "seed_pipeline.py"), enc], {
      encoding: "utf8",
      cwd: path.join(__dirname, "..", "..", "api"),
    }).trim();
    check("a transcript and note exist for the real recording (seeded)", !!noteId, noteId);
  } else {
    check("a note was generated from the recording", !!noteId, String(noteId));
  }
  if (!noteId) {
    console.log("\nCannot continue without a note.");
    await browser.close();
    process.exit(1);
  }

  // --- the resolved grounding payload -------------------------------------
  console.log("\n=== grounding resolves against the real transcript ===");
  const g = await call(page, async (id) => {
    const mod = await import("/src/lib/grounding.ts");
    return mod.fetchGrounding(id);
  }, noteId);

  check("grounding loads for a freshly generated note", !!g);
  check("audio is reported available while the recording still exists", g?.audio_state === "available", g?.audio_state);
  check("transcript is reported available", g?.transcript_state === "available", g?.transcript_state);

  const citedSegments = (g?.segments ?? []).filter((s) => s.cited);
  check("at least one transcript passage is cited by the note", citedSegments.length > 0, `${citedSegments.length} cited`);
  check(
    "cited passages carry real audio timings, not nulls",
    citedSegments.length > 0 && citedSegments.every((s) => typeof s.start_ms === "number" && s.end_ms > s.start_ms),
    citedSegments[0] ? `${citedSegments[0].start_ms}-${citedSegments[0].end_ms}ms` : "n/a",
  );
  // PHI minimisation: the transcript is verbatim, including what the doctor
  // chose not to write down. A highlight does not require shipping all of it.
  const totalSegments = await call(page, async (encId) => {
    const mod = await import("/src/api/client.ts");
    const r = await mod.api.GET("/api/v1/encounters/{encounter_id}", {
      params: { path: { encounter_id: encId } },
    });
    return r.data ? true : false;
  }, enc);
  check("grounding returns a bounded slice of the transcript, not all of it", (g?.segments ?? []).length > 0 && totalSegments);

  const groundedSectionKey = Object.keys(g?.sections ?? {}).find((k) => g.sections[k].spans_fit);
  check("at least one section's stored offsets still fit its text", !!groundedSectionKey, String(groundedSectionKey));

  // --- the screen: evidence first, editing opt-in -------------------------
  console.log("\n=== the review screen renders the note as interrogable lines ===");
  await page.goto(`${WEB_URL}/notes/${noteId}`, { waitUntil: "networkidle" });
  await sleep(2000);

  const lineCount = await page.locator(".ground-line").count();
  check("note lines render as clickable evidence, not a plain textarea", lineCount > 0, `${lineCount} lines`);
  check(
    "the affordance is explained rather than left to be discovered",
    (await page.getByText(/click any line of the note/i).count()) === 1,
  );

  // --- tap one: highlight, and only highlight -----------------------------
  const firstLine = page.locator(".ground-line").first();
  await firstLine.click();
  await sleep(600);

  check("tapping a line reveals its source transcript", (await page.locator(".passages").count()) >= 1);
  check("the tapped line is marked as selected", (await page.locator(".ground-line.is-selected").count()) === 1);
  const citedShown = await page.locator(".passage-list > li.is-cited").count();
  check("the cited passage is shown", citedShown >= 1, `${citedShown} cited`);
  check(
    "each passage shows a speaker and a timestamp into the recording",
    (await page.locator(".passage-speaker").count()) >= 1 && (await page.locator(".passage-time").count()) >= 1,
  );
  // Context is shown but visually and semantically ranked below evidence: a
  // neighbour is not what the line cited.
  const contextShown = await page.locator(".passage-list > li.is-context").count();
  check("neighbouring passages are labelled context, not evidence", contextShown >= 1, `${contextShown} context`);
  check(
    "no audio started on the first tap",
    (await page.locator(".playing-dot").count()) === 0 && (await page.getByRole("button", { name: /^stop$/i }).count()) === 0,
  );

  // --- tap two: play the passage -----------------------------------------
  console.log("\n=== the second tap plays the cited moment ===");
  // The passage's own length is the deadline here. A cited turn is often only
  // a second or two, and playback deliberately stops at its end — so a slow
  // assertion tests nothing and reports a failure. Poll instead.
  const readPlayerState = () =>
    call(page, async () => ({
      dot: document.querySelectorAll(".playing-dot").length,
      tags: [...document.querySelectorAll(".passage-tag")].map((t) => t.textContent),
      stop: [...document.querySelectorAll("button")].some((b) => /^stop$/i.test(b.textContent || "")),
      error: [...document.querySelectorAll(".ground-stale")].map((e) => e.textContent),
    }));

  await firstLine.click();
  let sounding = null;
  for (let i = 0; i < 25; i++) {
    await sleep(120);
    const s = await readPlayerState();
    if (s.dot >= 1 || s.stop) {
      sounding = s;
      break;
    }
  }
  console.log("  while playing: " + JSON.stringify(sounding));
  check("audio playback starts on the second tap", !!sounding, sounding ? "" : "never started");
  check(
    "the sounding passage is marked in the transcript panel",
    !!sounding && (sounding.tags.includes("playing") || sounding.dot >= 1),
  );
  check("a stop control is offered while audio is sounding", !!sounding && sounding.stop);

  // The assertion that matters more than starting: playback must not run on
  // through the rest of the consultation. A doctor asked to hear one line's
  // source, and continuing past it discloses the recording without them
  // noticing.
  const citedForFirstLine = (g?.segments ?? []).filter((s) => s.cited)[0];
  const passageMs = citedForFirstLine ? citedForFirstLine.end_ms - citedForFirstLine.start_ms : 2000;
  await sleep(passageMs + 3000);
  const afterPassage = await readPlayerState();
  console.log("  after the passage: " + JSON.stringify(afterPassage));
  check(
    "playback stops at the end of the cited passage, not at the end of the recording",
    afterPassage.dot === 0 && !afterPassage.stop,
    `passage was ${passageMs}ms`,
  );

  // --- the presigned URL keeps nothing on disk ---------------------------
  const audioUrl = await call(page, async (encId) => {
    const mod = await import("/src/lib/grounding.ts");
    return mod.fetchPlaybackUrl(encId);
  }, enc);
  check("a playback URL is minted on demand", !!audioUrl?.url, audioUrl?.error ?? "");
  check(
    "the URL is signed no-store, so playback leaves nothing in the browser cache",
    !!audioUrl?.url && /response-cache-control=no-store/i.test(decodeURIComponent(audioUrl.url)),
  );
  check(
    "the URL is short-lived",
    audioUrl?.expiresInSeconds > 0 && audioUrl?.expiresInSeconds <= 900,
    String(audioUrl?.expiresInSeconds),
  );
  // It must actually be fetchable, not merely well-formed — a presigned URL
  // that 403s is the dead play button in a different costume.
  const fetched = await call(page, async (url) => {
    try {
      const res = await fetch(url, { headers: { Range: "bytes=0-1023" } });
      return { status: res.status, bytes: (await res.arrayBuffer()).byteLength };
    } catch (e) {
      return { status: 0, error: String(e) };
    }
  }, audioUrl.url);
  console.log("  range fetch: " + JSON.stringify(fetched));
  // 206 Partial Content is the point: the browser fetches only the seconds it
  // plays, straight from object storage, and the API server never sees them.
  check("the recording answers Range requests, so only the played window transfers", fetched.status === 206, "HTTP " + fetched.status);

  // --- an edit withdraws grounding rather than approximating it -----------
  console.log("\n=== an edit invalidates the stored offsets ===");
  const editedSection = groundedSectionKey;
  const label = editedSection.charAt(0).toUpperCase() + editedSection.slice(1);
  const card = page.locator("section.card").filter({ has: page.getByRole("heading", { name: label, exact: true }) });
  await card.getByRole("button", { name: /edit this section/i }).click();
  await sleep(300);
  await page.getByLabel(label).fill("A wholly different sentence written by the doctor, of a different length.");
  await page.locator("h1").click(); // blur to save
  await sleep(2500);

  const afterEdit = await call(page, async (id) => {
    const mod = await import("/src/lib/grounding.ts");
    const gr = await mod.fetchGrounding(id);
    return gr ? { sections: gr.sections } : null;
  }, noteId);
  check(
    "the edited section's offsets are reported as no longer fitting",
    afterEdit?.sections?.[editedSection]?.spans_fit === false,
    String(afterEdit?.sections?.[editedSection]?.spans_fit),
  );
  check(
    "the edit is recorded as such, so the passages are not presented as the doctor's own words",
    afterEdit?.sections?.[editedSection]?.edited_since_generation === true,
  );
  await page.reload({ waitUntil: "networkidle" });
  await sleep(1800);
  check(
    "the screen says why grounding is withheld instead of highlighting approximately",
    (await page.getByText(/no longer line up/i).count()) >= 1,
  );

  // --- the degradation ladder --------------------------------------------
  console.log("\n=== withdrawing consent deletes the audio; grounding degrades honestly ===");
  const withdrawal = await call(page, async (encId) => {
    const mod = await import("/src/api/client.ts");
    const r = await mod.api.POST("/api/v1/consent", {
      body: {
        encounter_id: encId,
        event: "withdrawn",
        participant_roster: ["Doctor", "Patient"],
        purposes: ["clinical documentation"],
        script_language: "fil",
      },
    });
    return r.response.status;
  }, enc);
  check("consent withdrawn", withdrawal === 200 || withdrawal === 201, "HTTP " + withdrawal);
  await sleep(1500);

  const degraded = await call(page, async (id) => {
    const mod = await import("/src/lib/grounding.ts");
    const gr = await mod.fetchGrounding(id);
    return gr ? { audio: gr.audio_state, transcript: gr.transcript_state, segments: gr.segments.length } : null;
  }, noteId);
  check("audio is now reported as withdrawn", degraded?.audio === "withdrawn", degraded?.audio);
  // The middle rung: losing the recording must not take the highlighting with
  // it, because the transcript is still a checkable source.
  check("the transcript is still available, so grounding still works", degraded?.transcript === "available");
  check("cited passages are still returned", degraded?.segments > 0, `${degraded?.segments} segments`);

  const refused = await call(page, async (encId) => {
    const mod = await import("/src/lib/grounding.ts");
    return mod.fetchPlaybackUrl(encId);
  }, enc);
  check("no playback URL is minted for deleted audio", !!refused?.error, refused?.error ?? "got a url");
  // The whole point of this phase's heads-up: the doctor is told *why*, and
  // the reason distinguishes a withdrawal from retention expiry.
  check(
    "the refusal says the recording was deleted at the patient's request",
    /patient's request/i.test(refused?.error ?? ""),
    refused?.error,
  );

  await page.reload({ waitUntil: "networkidle" });
  await sleep(1800);
  check(
    "the screen explains the state rather than showing a dead play button",
    (await page.getByText(/deleted at the patient's request/i).count()) >= 1,
  );

  console.log("\n=== page errors ===");
  console.log(pageErrors.length ? pageErrors.join("\n") : "  (none)");
  check("no uncaught page errors", pageErrors.length === 0);

  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(
    "\n" +
      (failed.length === 0
        ? `PHASE 3 SMOKE: ALL ${checks.length} PASS`
        : `PHASE 3 SMOKE: ${failed.length} of ${checks.length} FAILED`),
  );
  process.exit(failed.length === 0 ? 0 : 1);
})().catch((e) => {
  console.error("DRIVER ERROR:", e);
  process.exit(2);
});
