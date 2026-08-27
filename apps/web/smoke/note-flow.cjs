/**
 * Phase 2.5 + 2.6 end-to-end: patient identity, and review/edit/file/sign.
 *
 * This is the first test that runs the WHOLE clinical journey as one flow —
 * consent, record, upload, transcribe, generate, link a patient, review,
 * edit, file, sign — against the real stack. Every earlier smoke test
 * covered one link in that chain; this one is the chain.
 *
 * Assertions map to P0-5 and P0-6:
 *   - name-first fuzzy search: exact links silently, near needs confirming,
 *     no match offers create-new with name + birthdate
 *   - recording is never blocked on identity
 *   - identity is re-confirmed at the moment the note is FILED, and a
 *     mismatch is rejected rather than silently corrected
 *   - APSO section order (not SOAP)
 *   - edits persist and signed notes become immutable
 *   - no state skipping; signing needs a PRC licence and captures identity
 *     and timestamp
 *   - the signed note becomes the patient's prior visit
 *
 * Prerequisites: Postgres, Redis, MinIO, API, Celery worker, Vite dev server.
 *
 * Run:
 *   PW_PATH=... MFA_SECRET=... node smoke/note-flow.cjs
 */
const crypto = require("node:crypto");

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

  // Two deliberately similar names: this is the case where a wrong link is
  // easy and expensive.
  const tag = Math.random().toString(36).slice(2, 7);
  const seeded = await call(
    page,
    async (t) => {
      const mod = await import("/src/api/client.ts");
      const make = async (name, birthdate) => {
        const res = await mod.api.POST("/api/v1/patients", { body: { name, birthdate } });
        return res.data?.id ?? null;
      };
      return {
        maria: await make(`Maria Santos Dela Cruz ${t}`, "1988-04-12"),
        mario: await make(`Mario Santos Dela Cruz ${t}`, "1975-01-20"),
      };
    },
    tag,
  );
  check("seeded two similar patients", !!seeded.maria && !!seeded.mario);

  // ------------------------------------------------------------------ 2.5
  console.log("\n=== 2.5 name-first fuzzy search (P0-6) ===");
  const results = await call(
    page,
    async (t) => {
      const mod = await import("/src/lib/patients.ts");
      return {
        exact: await mod.searchPatients(`Maria Santos Dela Cruz ${t}`),
        partial: await mod.searchPatients(`Maria Cruz ${t}`),
        typo: await mod.searchPatients(`Maria Santos Dela Cruzz ${t}`),
        nothing: await mod.searchPatients("Zzyzx Nonexistent Person"),
      };
    },
    tag,
  );

  check("exact name links silently", results.exact.kind === "exact", results.exact.kind);
  check(
    "a partial name still finds the patient",
    results.partial.kind === "near" &&
      results.partial.candidates.some((c) => c.full_name.startsWith("Maria Santos")),
    results.partial.kind,
  );
  check(
    "a typo finds the patient, labelled near not exact",
    results.typo.kind === "near" &&
      results.typo.candidates.some((c) => c.full_name.startsWith("Maria Santos")),
    results.typo.kind,
  );
  check("no match reports none, so create-new is offered", results.nothing.kind === "none");
  check(
    "candidates carry birthdate, which is what tells similar names apart",
    (results.partial.candidates ?? []).every((c) => !!c.birthdate),
  );

  // Search must not become a name-only dedup path (P0-6).
  const dedup = await call(
    page,
    async (t) => {
      const mod = await import("/src/api/client.ts");
      const wrong = await mod.api.POST("/api/v1/patients/match", {
        body: { name: `Maria Santos Dela Cruz ${t}`, birthdate: "1999-09-09" },
      });
      const right = await mod.api.POST("/api/v1/patients/match", {
        body: { name: `Maria Santos Dela Cruz ${t}`, birthdate: "1988-04-12" },
      });
      return { wrong: wrong.data?.match_type, right: right.data?.match_type };
    },
    tag,
  );
  check(
    "same name + wrong birthdate is NOT a dedup match",
    dedup.wrong === "none",
    "match_type=" + dedup.wrong,
  );
  check("same name + right birthdate IS a dedup match", dedup.right === "exact");

  // ------------------------------------------------------- full journey
  console.log("\n=== journey: consent -> record -> upload -> note ===");
  const enc = await call(page, async () => {
    const mod = await import("/src/api/client.ts");
    const res = await mod.api.POST("/api/v1/encounters", {
      body: { upload_idempotency_key: "smoke-note-" + Math.random().toString(36).slice(2) },
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
  });
  check("encounter created and consented", !!enc, enc);

  await page.goto(`${WEB_URL}/encounters/${enc}/record`, { waitUntil: "networkidle" });
  await sleep(1300);
  await page.getByRole("button", { name: /start recording/i }).click();
  await sleep(1000);
  check(
    "recording started with NO patient linked (P0-6: never blocked on identity)",
    (await page.locator(".rec-indicator").count()) === 1,
  );
  console.log("  recording 12s...");
  await sleep(12000);
  await page.getByRole("button", { name: /stop recording/i }).click();
  await sleep(2500);

  let noteId = null;
  for (let i = 0; i < 40; i++) {
    await sleep(2000);
    const snap = await call(
      page,
      async (encId) => {
        const mod = await import("/src/api/client.ts");
        const e = await mod.api.GET("/api/v1/encounters/{encounter_id}", {
          params: { path: { encounter_id: encId } },
        });
        return { status: e.data?.pipeline_status ?? null, noteId: e.data?.note_id ?? null };
      },
      enc,
    );
    if (i % 4 === 0) console.log(`  t+${(i + 1) * 2}s  pipeline=${snap.status}  note=${snap.noteId}`);
    if (snap.noteId) {
      noteId = snap.noteId;
      break;
    }
  }
  // note_id was added to EncounterOut in 2.6 precisely because this test
  // could not otherwise navigate to the screen it exists to exercise.
  check("encounter exposes its note id, so the review screen is reachable", !!noteId, String(noteId));

  console.log("\n=== 2.5 loose-session linking (P0-6) ===");
  const linked = await call(
    page,
    async (args) => {
      const mod = await import("/src/lib/patients.ts");
      return mod.linkEncounterToPatient(args.enc, args.patient);
    },
    { enc, patient: seeded.maria },
  );
  check("loose session linked to a patient after the fact", linked === true);

  // ------------------------------------------------------------------ 2.6
  console.log("\n=== 2.6 review, edit, file, sign (P0-5) ===");
  await page.goto(`${WEB_URL}/notes/${noteId}`, { waitUntil: "networkidle" });
  await sleep(1500);

  const headings = await page.evaluate(() =>
    [...document.querySelectorAll("h2")].map((h) => h.textContent),
  );
  const apso = ["Assessment", "Plan", "Subjective", "Objective"];
  const seen = headings.filter((h) => apso.includes(h));
  // P0-4 specifies APSO, not SOAP: the doctor's own conclusion is checked
  // first, because burying it under recounted symptoms is how a wrong
  // assessment gets signed.
  check("sections render in APSO order, not SOAP", JSON.stringify(seen) === JSON.stringify(apso), seen.join(" > "));

  const readAssessment = async () =>
    call(page, async (id) => {
      const mod = await import("/src/api/client.ts");
      const r = await mod.api.GET("/api/v1/notes/{note_id}", { params: { path: { note_id: id } } });
      return r.data?.assessment ?? "";
    }, noteId);

  const before = await readAssessment();
  await page.getByLabel("Assessment").fill(before + " Edited by the doctor.");
  await page.getByLabel("Plan").click(); // blur triggers the save
  await sleep(1600);
  const after = await readAssessment();
  check("an edit is persisted on blur", after.includes("Edited by the doctor."), after.slice(-42));

  // --- no state skipping (P0-5)
  const skip = await call(page, async (id) => {
    const mod = await import("/src/api/client.ts");
    const r = await mod.api.POST("/api/v1/notes/{note_id}/transition", {
      params: { path: { note_id: id } },
      body: { to_status: "signed", prc_license_number: "PRC-9" },
    });
    return r.response.status;
  }, noteId);
  check("signing straight from generated is rejected", skip === 409, "HTTP " + skip);

  // --- identity re-confirmed at filing (P0-6)
  const noConfirm = await call(page, async (id) => {
    const mod = await import("/src/api/client.ts");
    const r = await mod.api.POST("/api/v1/notes/{note_id}/transition", {
      params: { path: { note_id: id } },
      body: { to_status: "filed" },
    });
    return r.response.status;
  }, noteId);
  check("filing without confirming the patient is rejected", noConfirm === 409, "HTTP " + noConfirm);

  const wrongPatient = await call(
    page,
    async (args) => {
      const mod = await import("/src/api/client.ts");
      const r = await mod.api.POST("/api/v1/notes/{note_id}/transition", {
        params: { path: { note_id: args.id } },
        body: { to_status: "filed", confirmed_patient_id: args.wrong },
      });
      return r.response.status;
    },
    { id: noteId, wrong: seeded.mario },
  );
  // The case this exists to catch: a stale client showing the previous
  // patient. Silently preferring either side is how a note lands in the
  // wrong person's record.
  check("filing against the WRONG patient is rejected, not silently corrected", wrongPatient === 409, "HTTP " + wrongPatient);

  // --- the happy path through the state machine
  const walked = await call(
    page,
    async (args) => {
      const mod = await import("/src/api/client.ts");
      const steps = [];
      const step = async (body, label) => {
        const r = await mod.api.POST("/api/v1/notes/{note_id}/transition", {
          params: { path: { note_id: args.id } },
          body,
        });
        steps.push([label, r.response.status, r.data?.status ?? null]);
        return r.data;
      };
      await step({ to_status: "filed", confirmed_patient_id: args.patient }, "filed");
      await step({ to_status: "authenticated" }, "authenticated");
      await step({ to_status: "signed" }, "signed-without-licence");
      const note = await step(
        { to_status: "signed", prc_license_number: "PRC-0123456" },
        "signed",
      );
      return { steps, note };
    },
    { id: noteId, patient: seeded.maria },
  );
  console.log("  " + JSON.stringify(walked.steps));

  check("filed with a confirmed patient", walked.steps[0][1] === 200 && walked.steps[0][2] === "filed");
  check("authenticated", walked.steps[1][1] === 200 && walked.steps[1][2] === "authenticated");
  check("signing without a PRC licence is rejected", walked.steps[2][1] === 422, "HTTP " + walked.steps[2][1]);
  check("signed with a licence", walked.steps[3][1] === 200 && walked.steps[3][2] === "signed");
  check("signature captures the clinician", !!walked.note?.signed_by_clinician_id);
  check("signature captures the PRC licence", walked.note?.signed_prc_license_number === "PRC-0123456");
  check("signature captures a timestamp", !!walked.note?.signed_at);

  const editSigned = await call(page, async (id) => {
    const mod = await import("/src/api/client.ts");
    const r = await mod.api.PATCH("/api/v1/notes/{note_id}", {
      params: { path: { note_id: id } },
      body: { section: "assessment", text: "tampering after signature" },
    });
    return r.response.status;
  }, noteId);
  check("a signed note cannot be edited", editSigned === 409, "HTTP " + editSigned);

  // --- the signed note becomes longitudinal context (P0-5)
  const prior = await call(page, async (patientId) => {
    const mod = await import("/src/lib/patients.ts");
    return mod.fetchPriorVisit(patientId);
  }, seeded.maria);
  check(
    "the signed note becomes the patient's prior visit",
    !!prior && prior.assessment.includes("Edited by the doctor."),
    prior ? prior.assessment.slice(-42) : "null",
  );

  // --- the screen reflects the signed, immutable state
  await page.reload({ waitUntil: "networkidle" });
  await sleep(1400);
  check(
    "review screen shows the PRC licence it was signed under",
    (await page.getByText(/PRC-0123456/).count()) >= 1,
  );
  check("editing is disabled once signed", await page.getByLabel("Assessment").isDisabled());

  console.log("\n=== page errors ===");
  console.log(pageErrors.length ? pageErrors.join("\n") : "  (none)");
  check("no uncaught page errors", pageErrors.length === 0);

  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(
    "\n" +
      (failed.length === 0
        ? `PHASE 2.5/2.6 SMOKE: ALL ${checks.length} PASS`
        : `PHASE 2.5/2.6 SMOKE: ${failed.length} of ${checks.length} FAILED`),
  );
  process.exit(failed.length === 0 ? 0 : 1);
})().catch((e) => {
  console.error("DRIVER ERROR:", e);
  process.exit(2);
});
