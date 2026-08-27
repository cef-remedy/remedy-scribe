/**
 * Phase 2.3 end-to-end: the consent flow, driven through the real UI.
 *
 * This is the legal control, so it is tested as a journey rather than as
 * units. Every assertion here maps to a P0-1 clause:
 *
 *   - "blocks recording and presents the consent script (Filipino + English)
 *     before anything is captured"
 *   - "Given consent is given, when recording starts, then the spoken
 *     exchange is captured as the first segment"
 *   - "a new participant joins mid-recording... recording pauses until fresh
 *     consent is logged"
 *   - "a patient withdraws consent... processing stops and the associated
 *     audio is queued for deletion without undue delay"
 *   - and the PRD edge case: "the app works exactly the same way if I decline
 *     to record"
 *
 * The decline path is checked on a *separate encounter*, because the ledger
 * is append-only — a decline followed by a grant on the same encounter would
 * leave consent valid and prove nothing.
 *
 * Run:
 *   PW_PATH=... MFA_SECRET=... node smoke/consent-flow.cjs
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

const newEncounter = (page) =>
  page.evaluate(async () => {
    const mod = await import("/src/api/client.ts");
    const res = await mod.api.POST("/api/v1/encounters", {
      body: { upload_idempotency_key: "smoke-consent-" + Math.random().toString(36).slice(2) },
    });
    if (!res.data) throw new Error("encounter create failed: " + res.response.status);
    return res.data.id;
  });

const ledgerState = (page, encId) =>
  page.evaluate(async (id) => {
    const mod = await import("/src/api/client.ts");
    const res = await mod.api.GET("/api/v1/consent/{encounter_id}", {
      params: { path: { encounter_id: id } },
    });
    return res.data;
  }, encId);

const storedChunkCount = (page, sessionId) =>
  page.evaluate(async (sid) => {
    const db = await new Promise((resolve, reject) => {
      // No version argument on purpose: opening at a pinned version throws
      // VersionError once the app's own schema moves past it, and a read-only
      // probe has no business dictating the schema anyway. This test was
      // unrunnable from the moment Phase 2.2 bumped the store to v2.
      const req = indexedDB.open("remedy-scribe");
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    const all = await new Promise((resolve, reject) => {
      const req = db.transaction("chunks", "readonly").objectStore("chunks").getAll();
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    db.close();
    return all.filter((c) => c.sessionId === sid).length;
  }, sessionId);

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
  await sleep(700);
  check("signed in", !page.url().includes("/login"));

  // ---------------------------------------------------------------- decline
  console.log("\n=== 1. decline path (own encounter — the ledger is append-only) ===");
  const declineEnc = await newEncounter(page);
  await page.goto(`${WEB_URL}/encounters/${declineEnc}/record`, { waitUntil: "networkidle" });
  await sleep(1000);

  check(
    "blocked, and offers a route into the consent flow",
    (await page.getByRole("button", { name: /capture consent/i }).count()) === 1,
  );
  await page.getByRole("button", { name: /capture consent/i }).click();
  await page.waitForURL(/\/consent$/, { timeout: 10000 });
  await sleep(600);

  const warnText = await page.locator(".banner--warn").first().textContent();
  check(
    "warns the script is not cleared by counsel",
    /not been cleared by counsel/i.test(warnText || ""),
  );

  // The script and the decision buttons are gated behind the roster step on
  // purpose: a decline is a *response to the script*, so it must not be
  // reachable before the script has been presented (P0-1 requires the script
  // to be presented, not merely available).
  check(
    "decline is not offered before the script is presented",
    (await page.getByRole("button", { name: /patient declined/i }).count()) === 0,
  );
  await page.getByRole("button", { name: /continue to the script/i }).click();
  await sleep(500);

  check(
    "presents BOTH languages (P0-1: Filipino + English)",
    (await page.locator(".script").count()) === 2,
  );
  check(
    "Filipino script text is present",
    (await page.getByText(/Pumapayag po ba kayo/).count()) === 1,
  );
  check(
    "English script text is present",
    (await page.getByText(/Do you agree to us recording/).count()) === 1,
  );
  check(
    "required participants are locked, not optional",
    (await page.locator(".roster input[disabled]").count()) === 2,
  );
  check(
    "microphone never touched on the consent screen",
    (await page.locator(".rec-indicator").count()) === 0,
  );

  await page.getByRole("button", { name: /patient declined/i }).click();
  await sleep(1200);
  check("decline confirmation shown", (await page.getByText(/Recording declined/).count()) === 1);
  check(
    "decline states the app still works normally (PRD edge case)",
    (await page.getByText(/consultation continues as normal/i).count()) === 1,
  );

  const declined = await ledgerState(page, declineEnc);
  check("ledger records 'declined'", declined?.latest_event === "declined", JSON.stringify(declined));
  check("still cannot record after declining", declined?.can_record === false);

  // ------------------------------------------------------------------ grant
  console.log("\n=== 2. grant path, on a fresh encounter ===");
  const enc = await newEncounter(page);
  await page.goto(`${WEB_URL}/encounters/${enc}/consent`, { waitUntil: "networkidle" });
  await sleep(600);

  // Add a companion to the roster: RA 4200 needs every party named.
  await page.getByLabel(/Companion \/ relative/).check();
  await page.getByRole("button", { name: /continue to the script/i }).click();
  await sleep(400);
  await page.getByRole("button", { name: /patient agreed/i }).click();

  await page.waitForURL(/\/record\?confirm=1$/, { timeout: 10000 });
  await sleep(1000);
  check("redirected to recording with the confirmation flag", /confirm=1/.test(page.url()));

  const granted = await ledgerState(page, enc);
  check("ledger records 'given'", granted?.latest_event === "given");
  check("script language recorded", granted?.script_language === "fil", granted?.script_language);
  check("can now record", granted?.can_record === true);

  await page.getByRole("button", { name: /start recording/i }).click();
  await sleep(1500);
  check("recording started", (await page.locator(".rec-indicator").count()) === 1);
  check(
    "spoken-confirmation prompt shown (P0-1: first segment)",
    (await page.getByText(/Say this now, for the record/i).count()) === 1,
  );
  check(
    "the prompt names who is in the room",
    (await page.getByText(/Nasa kwarto po/).count()) === 1,
  );

  // ------------------------------------------------------- mid-visit pause
  console.log("\n=== 3. mid-visit re-consent (P0-1) ===");
  await sleep(6000); // let a chunk or two land first
  await page.getByRole("button", { name: /someone joined/i }).click();
  await sleep(1200);

  const pausedText = await page.locator(".rec-indicator").textContent();
  check("indicator switches to paused, still visible", /Paused/i.test(pausedText || ""), (pausedText || "").trim());
  check(
    "paused state is visually distinct",
    (await page.locator(".rec-indicator.is-paused").count()) === 1,
  );
  check(
    "resume is gated behind fresh consent, not offered directly",
    (await page.getByText(/Fresh consent needed/i).count()) === 1,
  );

  await page.getByRole("button", { name: /they consented — resume/i }).click();
  await sleep(2500);

  const resumedText = await page.locator(".rec-indicator").textContent();
  check("resumed after fresh consent", /Recording/.test(resumedText || "") && !/Paused/i.test(resumedText || ""));

  const afterReconsent = await ledgerState(page, enc);
  check(
    "a second 'given' entry was appended for the new roster",
    (afterReconsent?.entry_count ?? 0) >= 2,
    "entries=" + afterReconsent?.entry_count,
  );

  const kv = await page.evaluate(() =>
    Object.fromEntries(
      [...document.querySelectorAll(".kv")].flatMap((dl) => {
        const dts = [...dl.querySelectorAll("dt")].map((d) => d.textContent);
        const dds = [...dl.querySelectorAll("dd")].map((d) => d.textContent);
        return dts.map((k, i) => [k, dds[i]]);
      }),
    ),
  );
  console.log("  detail panel: " + JSON.stringify(kv));
  check("paused time is reported separately", "Paused" in kv, kv["Paused"]);
  // The interaction that would break naively: a deliberate pause must not be
  // counted as lost audio.
  const missingSec = parseInt((kv["Missing"] || "0:00").split(":")[1] || "0", 10);
  check("the pause is NOT reported as missing audio", missingSec <= 1, "missing=" + kv["Missing"]);

  await sleep(5000);

  // -------------------------------------------------------------- withdrawal
  console.log("\n=== 4. withdrawal (P0-1) ===");
  const beforeWithdraw = await storedChunkCount(page, enc);
  check("audio existed locally before withdrawal", beforeWithdraw >= 1, beforeWithdraw + " chunks");

  await page.getByRole("button", { name: /patient withdrew consent/i }).click();
  await sleep(3000);

  const afterWithdraw = await storedChunkCount(page, enc);
  check("local audio deleted from this laptop", afterWithdraw === 0, afterWithdraw + " chunks");

  // Target the withdrawal banner by its content, not by position: the page
  // legitimately renders several warn-toned banners (the audio-settings
  // mismatch notice is one), and `.last()` picked the wrong one.
  const withdrawText = await page
    .locator(".banner--warn", { hasText: /Withdrawal recorded/i })
    .first()
    .textContent()
    .catch(() => "");
  check("withdrawal outcome reported to the doctor", /Withdrawal recorded/i.test(withdrawText || ""));
  // The honesty requirement: never imply an instant abort, because a running
  // Celery task cannot be reliably killed.
  check(
    "says 'next stage boundary', not 'instantly'",
    /next stage boundary/i.test(withdrawText || ""),
    (withdrawText || "").slice(-90),
  );

  const withdrawn = await ledgerState(page, enc);
  check("ledger records 'withdrawn'", withdrawn?.latest_event === "withdrawn");
  check("recording blocked again", withdrawn?.can_record === false);
  check("indicator gone after withdrawal", (await page.locator(".rec-indicator").count()) === 0);

  console.log("\n=== page errors ===");
  console.log(pageErrors.length ? pageErrors.join("\n") : "  (none)");
  check("no uncaught page errors", pageErrors.length === 0);

  await browser.close();
  const failed = checks.filter((c) => !c.ok);
  console.log(
    "\n" +
      (failed.length === 0
        ? `PHASE 2.3 SMOKE: ALL ${checks.length} PASS`
        : `PHASE 2.3 SMOKE: ${failed.length} of ${checks.length} FAILED`),
  );
  process.exit(failed.length === 0 ? 0 : 1);
})().catch((e) => {
  console.error("DRIVER ERROR:", e);
  process.exit(2);
});
