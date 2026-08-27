/**
 * Phase 2.2 end-to-end smoke test: real recording in a real browser.
 *
 * The unit tests cover crypto and storage. Everything they cannot reach —
 * getUserMedia, MediaRecorder, AudioWorklet, the wake lock, and the P0-1
 * consent gate wired to the live API — is only meaningfully testable
 * against a real Chromium, so it is tested there rather than mocked. A
 * jsdom MediaRecorder would only ever prove the mock works; that is the
 * exact gap that hid a real httpx bug in Phase 1.3.
 *
 * What it asserts, ordered by what would hurt most to get wrong:
 *   1. **Recording is blocked with no consent.** This is P0-1 and it is a
 *      legal control. If it regresses, the app records patients unlawfully.
 *   2. Recording starts once consent exists in the ledger, and the
 *      persistent indicator (also P0-1) is visible while it runs.
 *   3. Real chunks land in IndexedDB, and what is stored is ciphertext —
 *      plaintext audio must never reach disk (P0-2).
 *   4. The audio clock tracks wall clock, i.e. no silent gaps.
 *   5. Consent withdrawal re-blocks recording.
 *
 * Prerequisites: API on :8000 seeded with a clinician, Vite dev server on
 * :5173. See the Phase 2.2 progress writeup for the exact commands.
 *
 * Run:
 *   PW_PATH=... MFA_SECRET=... node smoke/record-flow.cjs
 */
const crypto = require("node:crypto");

const PW_PATH = process.env.PW_PATH || "playwright";
const WEB_URL = process.env.WEB_URL || "http://localhost:5173";
const API_URL = process.env.API_URL || "http://localhost:8000";
const MFA_SECRET = process.env.MFA_SECRET;
const EMAIL = process.env.SMOKE_EMAIL || "doc@example.com";
const PASSWORD = process.env.SMOKE_PASSWORD || "smoke-test-password";

if (!MFA_SECRET) {
  console.error("MFA_SECRET is required (base32 TOTP secret of the seeded clinician).");
  process.exit(2);
}

const { chromium } = require(PW_PATH);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* RFC 6238 TOTP, hand-rolled to avoid a dependency for a test-only need. */
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
  const offset = hmac[hmac.length - 1] & 0x0f;
  const code =
    ((hmac[offset] & 0x7f) << 24) |
    ((hmac[offset + 1] & 0xff) << 16) |
    ((hmac[offset + 2] & 0xff) << 8) |
    (hmac[offset + 3] & 0xff);
  return String(code % 1e6).padStart(6, "0");
}

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok });
  console.log("  " + (ok ? "PASS" : "FAIL") + "  " + name + (detail ? "  -- " + detail : ""));
}

/** Reads the browser's IndexedDB directly — the durability claim, verified. */
function readStoredChunks(page, sessionId) {
  return page.evaluate(async (sid) => {
    const db = await new Promise((resolve, reject) => {
      const req = indexedDB.open("remedy-scribe", 1);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    const all = await new Promise((resolve, reject) => {
      const req = db.transaction("chunks", "readonly").objectStore("chunks").getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    db.close();
    const mine = all.filter((c) => c.sessionId === sid);
    return {
      count: mine.length,
      totalBytes: mine.reduce((n, c) => n + c.byteLength, 0),
      hasIv: mine.every((c) => c.iv && c.iv.byteLength === 12),
      hasCiphertext: mine.every((c) => c.ciphertext && c.ciphertext.byteLength > 0),
      // A WebM/Opus file starts with the EBML magic 0x1A45DFA3. If that shows
      // up in what we stored, the bytes are plaintext audio, not ciphertext.
      looksLikePlaintextAudio: mine.some((c) => {
        const head = new Uint8Array(c.ciphertext.slice(0, 4));
        return head[0] === 0x1a && head[1] === 0x45 && head[2] === 0xdf && head[3] === 0xa3;
      }),
      seqs: mine.map((c) => c.seq),
      mimeTypes: [...new Set(mine.map((c) => c.mimeType))],
    };
  }, sessionId);
}

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

  // --- sign in, then create an encounter via the API using the same session
  console.log("\n=== setup: sign in ===");
  await page.goto(WEB_URL, { waitUntil: "networkidle" });
  await page.fill("#email", EMAIL);
  await page.fill("#password", PASSWORD);
  await page.fill("#mfa", totp(MFA_SECRET));
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 15000 });
  await sleep(800);
  check("signed in", !page.url().includes("/login"));

  const encounterId = await page.evaluate(async (apiUrl) => {
    // Reuse the app's own in-memory access token by going through its client.
    const mod = await import("/src/api/client.ts");
    const res = await mod.api.POST("/api/v1/encounters", {
      body: { upload_idempotency_key: "smoke-record-" + Date.now() },
    });
    if (!res.data) throw new Error("could not create encounter: " + res.response.status);
    void apiUrl;
    return res.data.id;
  }, API_URL);
  check("encounter created", !!encounterId, encounterId);

  // --- 1. P0-1: recording must be blocked with no consent in the ledger
  console.log("\n=== 1. consent gate blocks recording (P0-1) ===");
  await page.goto(`${WEB_URL}/encounters/${encounterId}/record`, { waitUntil: "networkidle" });
  await sleep(1200);

  const blockedText = await page.locator(".banner--error").first().textContent().catch(() => "");
  check("blocked banner shown", /Recording is blocked/.test(blockedText || ""), (blockedText || "").slice(0, 70));
  check(
    "no start button offered without consent",
    (await page.getByRole("button", { name: /start recording/i }).count()) === 0,
  );
  check(
    "recording indicator absent before consent",
    (await page.locator(".rec-indicator").count()) === 0,
  );

  // --- 2. grant consent in the ledger, then record for real
  console.log("\n=== 2. consent granted -> real recording ===");
  const consentStatus = await page.evaluate(async (encId) => {
    const mod = await import("/src/api/client.ts");
    const res = await mod.api.POST("/api/v1/consent", {
      body: {
        encounter_id: encId,
        event: "given",
        participant_roster: ["doctor", "patient"],
        purposes: ["clinical documentation"],
        script_language: "fil",
      },
    });
    return res.response.status;
  }, encounterId);
  check("consent 'given' appended to the ledger", consentStatus === 201, "HTTP " + consentStatus);

  await page.reload({ waitUntil: "networkidle" });
  await sleep(1200);
  check(
    "start button appears once consent exists",
    (await page.getByRole("button", { name: /start recording/i }).count()) === 1,
  );

  await page.getByRole("button", { name: /start recording/i }).click();
  await sleep(1500);

  check("recording indicator visible (P0-1)", (await page.locator(".rec-indicator").count()) === 1);
  const indicatorText = await page.locator(".rec-indicator").textContent();
  check("indicator says Recording", /Recording/.test(indicatorText || ""), (indicatorText || "").trim());

  // Record long enough for several 5s chunks to flush.
  console.log("  recording for 17s to flush multiple chunks...");
  await sleep(17000);

  const liveStats = await page.evaluate(() => {
    const cells = [...document.querySelectorAll(".kv dd")].map((d) => d.textContent);
    return cells;
  });
  console.log("  live panel: " + JSON.stringify(liveStats));

  await page.getByRole("button", { name: /stop recording/i }).click();
  await sleep(2500);

  check(
    "indicator disappears after stop",
    (await page.locator(".rec-indicator").count()) === 0,
  );

  // --- 3. what actually landed on disk
  console.log("\n=== 3. stored chunks (P0-2: encrypted before disk) ===");
  const stored = await readStoredChunks(page, encounterId);
  console.log("  " + JSON.stringify(stored));

  check("multiple chunks written", stored.count >= 2, stored.count + " chunks");
  check("chunks carry bytes", stored.totalBytes > 0, stored.totalBytes + " B");
  check("every chunk has a 12-byte IV", stored.hasIv);
  check("every chunk has ciphertext", stored.hasCiphertext);
  // The load-bearing assertion: if the WebM magic bytes appear, we stored
  // raw audio and "encrypted on device" is false.
  check("stored bytes are NOT plaintext audio", stored.looksLikePlaintextAudio === false);
  check(
    "sequence numbers are contiguous from 0",
    JSON.stringify(stored.seqs.slice().sort((a, b) => a - b)) ===
      JSON.stringify(stored.seqs.map((_, i) => i)),
    JSON.stringify(stored.seqs),
  );
  check("a real container was negotiated", stored.mimeTypes.length === 1, stored.mimeTypes.join());

  // --- 4. no silent gaps
  console.log("\n=== 4. gap detection ===");
  const gapInfo = await page.evaluate(() => {
    const dds = [...document.querySelectorAll(".kv dd")].map((d) => d.textContent || "");
    return { elapsed: dds[0], captured: dds[1], missing: dds[2] };
  });
  console.log("  " + JSON.stringify(gapInfo));
  const missingSec = parseInt((gapInfo.missing || "0:00").split(":")[1] || "0", 10);
  check("under 1s of audio missing on an undisturbed run", missingSec <= 1, gapInfo.missing);
  check(
    "no gap banner on an undisturbed run",
    (await page.getByText(/gap in the audio|gaps in the audio/).count()) === 0,
  );

  // --- 5. withdrawal re-blocks
  console.log("\n=== 5. withdrawal re-blocks recording ===");
  await page.evaluate(async (encId) => {
    const mod = await import("/src/api/client.ts");
    await mod.api.POST("/api/v1/consent", {
      body: {
        encounter_id: encId,
        event: "withdrawn",
        participant_roster: [],
        purposes: [],
        script_language: "fil",
      },
    });
  }, encounterId);

  await page.reload({ waitUntil: "networkidle" });
  await sleep(1200);
  const withdrawnText = await page.locator(".banner--error").first().textContent().catch(() => "");
  check(
    "withdrawal blocks recording again",
    /withdrawn/i.test(withdrawnText || ""),
    (withdrawnText || "").slice(0, 80),
  );
  check(
    "no start button after withdrawal",
    (await page.getByRole("button", { name: /start recording/i }).count()) === 0,
  );

  console.log("\n=== page errors ===");
  console.log(pageErrors.length ? pageErrors.join("\n") : "  (none)");
  check("no uncaught page errors", pageErrors.length === 0);

  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(
    "\n" +
      (failed.length === 0
        ? `PHASE 2.2 SMOKE: ALL ${checks.length} PASS`
        : `PHASE 2.2 SMOKE: ${failed.length} of ${checks.length} FAILED`),
  );
  process.exit(failed.length === 0 ? 0 : 1);
})().catch((e) => {
  console.error("DRIVER ERROR:", e);
  process.exit(2);
});
