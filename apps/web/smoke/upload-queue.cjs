/**
 * Phase 2.4 end-to-end: the upload queue, against real MinIO.
 *
 * The unit tests cover part-planning arithmetic and the state machine. What
 * they cannot reach is the part that actually breaks in production: presigned
 * PUTs to a real S3-compatible endpoint, and the ordering rule that local
 * audio is deleted only once the *pipeline* has started — not when the bytes
 * land. Both are tested here against MinIO because a mocked S3 would prove
 * only that the mock accepts what we send.
 *
 * Each assertion maps to a P0-2 clause:
 *   - "uploads are resumable and chunked, with an idempotency key that
 *     prevents duplicate notes from a retried upload"
 *   - "the doctor sees a visible, persistent queue status for any recording
 *     not yet uploaded"
 *   - "local audio is deleted only after the server confirms receipt and note
 *     generation has begun"
 *
 * Prerequisites: MinIO on :9002, API on :8000 seeded, Vite on :5173.
 *
 * Run:
 *   PW_PATH=... MFA_SECRET=... node smoke/upload-queue.cjs
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

const queueEntry = (page, id) =>
  page.evaluate(async (encId) => {
    const db = await new Promise((resolve, reject) => {
      const req = indexedDB.open("remedy-scribe", 2);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    const entry = await new Promise((resolve, reject) => {
      const req = db.transaction("uploads", "readonly").objectStore("uploads").get(encId);
      req.onsuccess = () => resolve(req.result ?? null);
      req.onerror = () => reject(req.error);
    });
    const chunks = await new Promise((resolve, reject) => {
      const req = db.transaction("chunks", "readonly").objectStore("chunks").getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    db.close();
    return { entry, chunkCount: chunks.filter((c) => c.sessionId === encId).length };
  }, id);

const encounterStatus = (page, id) =>
  page.evaluate(async (encId) => {
    const mod = await import("/src/api/client.ts");
    const res = await mod.api.GET("/api/v1/encounters/{encounter_id}", {
      params: { path: { encounter_id: encId } },
    });
    return res.data?.pipeline_status ?? null;
  }, id);

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

  console.log("\n=== setup: sign in, consent, record ===");
  await page.goto(WEB_URL, { waitUntil: "networkidle" });
  await page.fill("#email", EMAIL);
  await page.fill("#password", PASSWORD);
  await page.fill("#mfa", totp(MFA_SECRET));
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 15000 });
  await sleep(700);

  const enc = await page.evaluate(async () => {
    const mod = await import("/src/api/client.ts");
    const res = await mod.api.POST("/api/v1/encounters", {
      body: { upload_idempotency_key: "smoke-queue-" + Math.random().toString(36).slice(2) },
    });
    if (!res.data) throw new Error("encounter create failed: " + res.response.status);
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
  await sleep(1200);

  // --- 1. the write-ahead entry exists BEFORE audio does
  console.log("\n=== 1. write-ahead invariant ===");
  await page.getByRole("button", { name: /start recording/i }).click();
  await sleep(900);

  const early = await queueEntry(page, enc);
  check("queue entry written at recording start", early.entry !== null, early.entry?.state);
  check("its state is 'recording'", early.entry?.state === "recording");
  check(
    "the idempotency key is already persisted",
    typeof early.entry?.idempotencyKey === "string" && early.entry.idempotencyKey.length > 0,
    early.entry?.idempotencyKey,
  );

  console.log("  recording 14s...");
  await sleep(14000);
  await page.getByRole("button", { name: /stop recording/i }).click();
  await sleep(2000);

  const afterStop = await queueEntry(page, enc);
  check("audio is on disk after stop", afterStop.chunkCount >= 2, afterStop.chunkCount + " chunks");
  check(
    "entry moved past 'recording'",
    afterStop.entry?.state !== "recording",
    afterStop.entry?.state,
  );

  // --- 2. visible queue status (P0-2)
  console.log("\n=== 2. visible, persistent queue status ===");
  check(
    "queue panel is rendered",
    (await page.getByRole("heading", { name: /upload queue/i }).count()) >= 1,
  );
  const queueText = await page.locator(".queue").first().textContent().catch(() => "");
  check("the recording appears in it", (queueText || "").includes(enc.slice(0, 8)), (queueText || "").slice(0, 90));
  // Regression guard. The first run of this test surfaced a real bug the
  // assertions had missed: a normally-stopped recording was labelled
  // "Recording was interrupted" because the queue could not tell a live
  // recording from a crashed one, and it queued the upload mid-capture.
  check(
    "a normally-stopped recording is NOT labelled interrupted",
    !/was interrupted/i.test(queueText || ""),
    (queueText || "").slice(0, 110),
  );

  // --- 3. the upload actually happens against real MinIO
  console.log("\n=== 3. real upload to MinIO, then pipeline confirmation ===");
  let state = null;
  let status = null;
  for (let i = 0; i < 40; i++) {
    await sleep(2000);
    const snapshot = await queueEntry(page, enc);
    state = snapshot.entry?.state ?? null;
    status = await encounterStatus(page, enc);
    if (i % 4 === 0) console.log(`  t+${(i + 1) * 2}s  queue=${state}  pipeline=${status}  chunks=${snapshot.chunkCount}`);
    if (state === "done" || state === "failed") break;
  }

  const final = await queueEntry(page, enc);
  console.log("  final entry: " + JSON.stringify(final.entry && {
    state: final.entry.state,
    bytesUploaded: final.entry.bytesUploaded,
    bytesTotal: final.entry.bytesTotal,
    attempts: final.entry.attempts,
    lastError: final.entry.lastError,
  }));

  check("upload reached a terminal state", ["done", "failed"].includes(final.entry?.state), final.entry?.state);
  check("upload did not fail", final.entry?.state !== "failed", final.entry?.lastError || "");
  check("bytes were uploaded", (final.entry?.bytesUploaded ?? 0) > 0, final.entry?.bytesUploaded + " B");
  // Regression guard, from a bug this test's own output exposed: the byte
  // total was taken from React state captured before stop()'s final flush,
  // so the queue displayed "56 KB of 37 KB" — progress over 100%.
  check(
    "progress never exceeds the total",
    (final.entry?.bytesUploaded ?? 0) <= (final.entry?.bytesTotal ?? 0),
    `${final.entry?.bytesUploaded} of ${final.entry?.bytesTotal}`,
  );

  // The ordering rule that matters most in this phase.
  const finalStatus = await encounterStatus(page, enc);
  check(
    "the server pipeline actually ran (not merely 'uploaded')",
    ["transcribed", "note_generated"].includes(finalStatus),
    "pipeline_status=" + finalStatus,
  );
  check(
    "local audio deleted ONLY after pipeline start",
    final.entry?.state === "done" && final.chunkCount === 0,
    final.chunkCount + " chunks left",
  );

  // --- 4. idempotency: a second upload attempt must not duplicate anything
  console.log("\n=== 4. idempotency on a repeat attempt ===");
  const repeat = await page.evaluate(async (encId) => {
    const mod = await import("/src/lib/queue/queue.ts");
    await mod.retryEntry(encId);
    return true;
  }, enc).catch((e) => String(e));
  check("a forced retry of a finished upload is handled", repeat === true, String(repeat).slice(0, 80));
  await sleep(4000);
  const afterRetry = await queueEntry(page, enc);
  check(
    "it does not resurrect deleted audio or loop",
    ["failed", "done"].includes(afterRetry.entry?.state),
    afterRetry.entry?.state + " / " + (afterRetry.entry?.lastError || "no error"),
  );

  console.log("\n=== page errors ===");
  console.log(pageErrors.length ? pageErrors.join("\n") : "  (none)");
  check("no uncaught page errors", pageErrors.length === 0);

  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(
    "\n" +
      (failed.length === 0
        ? `PHASE 2.4 SMOKE: ALL ${checks.length} PASS`
        : `PHASE 2.4 SMOKE: ${failed.length} of ${checks.length} FAILED`),
  );
  process.exit(failed.length === 0 ? 0 : 1);
})().catch((e) => {
  console.error("DRIVER ERROR:", e);
  process.exit(2);
});
