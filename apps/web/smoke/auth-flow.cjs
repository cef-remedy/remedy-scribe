/**
 * Phase 2.1 end-to-end smoke test: drives a real browser through the real
 * auth flow against the real API.
 *
 * This exists because every individual piece can be correct while the
 * whole thing still fails — CORS, the httpOnly cookie, `credentials:
 * "include"`, and the generated client all have to agree, and three of
 * those fail *silently* in ways that look like someone else's bug.
 *
 * What it asserts, in order of what would hurt most to get wrong:
 *   1. Login works against the live API with a live TOTP code.
 *   2. The refresh cookie is set AND is httpOnly — the security property
 *      the whole move off expo-secure-store was for (decision 0024).
 *   3. The access token is NOT in any JS-readable storage. If this ever
 *      regresses, the app keeps working perfectly while quietly becoming
 *      XSS-exfiltratable.
 *   4. A full page reload restores the session from the cookie alone.
 *      This is the "resume" behaviour in checklist 2.1, and it is only
 *      possible because the token is in an httpOnly cookie rather than in
 *      memory we just threw away.
 *   5. Logout clears both the cookie and the in-memory token.
 *
 * Prerequisites (see the Phase 2.1 progress writeup for the exact commands):
 *   - API on :8000 seeded with a known MFA-enrolled clinician
 *   - Vite dev server on :5173
 *
 * Run:
 *   PW_PATH=... MFA_SECRET=... node smoke/auth-flow.cjs
 *
 * .cjs, not .js: the web app's package.json sets "type": "module", and
 * this test-only script wants CommonJS require() for playwright.
 */
const crypto = require("node:crypto");

const PW_PATH = process.env.PW_PATH || "playwright";
const WEB_URL = process.env.WEB_URL || "http://localhost:5173";
const MFA_SECRET = process.env.MFA_SECRET;
const EMAIL = process.env.SMOKE_EMAIL || "doc@example.com";
const PASSWORD = process.env.SMOKE_PASSWORD || "smoke-test-password";
const COOKIE_NAME = process.env.COOKIE_NAME || "remedy_refresh";

if (!MFA_SECRET) {
  console.error("MFA_SECRET is required (the base32 TOTP secret of the seeded clinician).");
  process.exit(2);
}

const { chromium } = require(PW_PATH);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* RFC 6238 TOTP. Hand-rolled rather than pulling a dependency into the
 * web app for a test-only concern. */
function base32Decode(input) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of input.replace(/=+$/, "").toUpperCase()) {
    const idx = alphabet.indexOf(ch);
    if (idx === -1) continue;
    bits += idx.toString(2).padStart(5, "0");
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

function totp(secret, step = 30, digits = 6) {
  const counter = Math.floor(Date.now() / 1000 / step);
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
  return String(code % 10 ** digits).padStart(digits, "0");
}

const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok, detail });
  console.log("  " + (ok ? "PASS" : "FAIL") + "  " + name + (detail ? "  -- " + detail : ""));
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  const consoleErrors = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  page.on("pageerror", (e) => consoleErrors.push("PAGEERROR: " + e.message));

  console.log("\n=== 1. load + redirect to login ===");
  await page.goto(WEB_URL, { waitUntil: "networkidle" });
  check("unauthenticated visit lands on /login", page.url().includes("/login"), page.url());
  check("login form rendered", (await page.locator("#email").count()) === 1);

  console.log("\n=== 2. login with a live TOTP code ===");
  await page.fill("#email", EMAIL);
  await page.fill("#password", PASSWORD);
  await page.fill("#mfa", totp(MFA_SECRET));
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 15000 }).catch(() => {});
  await sleep(1200);

  const signedIn = !page.url().includes("/login");
  check("signed in and routed off /login", signedIn, page.url());
  if (!signedIn) {
    const err = await page.locator(".field-error").textContent().catch(() => null);
    console.log("    login error shown: " + err);
  }
  check(
    "worklist headings rendered from the typed client",
    (await page.getByText("Loose sessions").count()) > 0,
  );

  console.log("\n=== 3. the refresh cookie ===");
  const cookies = await context.cookies();
  const refresh = cookies.find((c) => c.name === COOKIE_NAME);
  check("refresh cookie present", !!refresh);
  check("refresh cookie is httpOnly", !!refresh && refresh.httpOnly === true);
  check(
    "refresh cookie scoped to /api/v1/auth",
    !!refresh && refresh.path === "/api/v1/auth",
    refresh ? refresh.path : "n/a",
  );

  console.log("\n=== 4. no token in JS-readable storage ===");
  const storage = await page.evaluate(() => ({
    local: Object.entries({ ...localStorage }),
    session: Object.entries({ ...sessionStorage }),
    docCookie: document.cookie,
  }));
  const asText = JSON.stringify(storage).toLowerCase();
  check("localStorage holds no token", !/token|bearer|eyj/.test(JSON.stringify(storage.local).toLowerCase()));
  check("sessionStorage holds no token", !/token|bearer|eyj/.test(JSON.stringify(storage.session).toLowerCase()));
  check(
    "refresh cookie invisible to document.cookie",
    !storage.docCookie.includes(COOKIE_NAME),
    "document.cookie=" + JSON.stringify(storage.docCookie),
  );
  check("no JWT anywhere in web storage", !asText.includes("eyj"));

  console.log("\n=== 5. reload restores the session from the cookie alone ===");
  await page.reload({ waitUntil: "networkidle" });
  await sleep(1500);
  check(
    "still signed in after a full reload",
    !page.url().includes("/login"),
    page.url(),
  );
  check(
    "worklist re-rendered after reload",
    (await page.getByText("Loose sessions").count()) > 0,
  );

  console.log("\n=== 6. logout ===");
  await page.click("text=Sign out");
  await page.waitForURL((u) => u.pathname.includes("/login"), { timeout: 10000 }).catch(() => {});
  await sleep(800);
  check("routed back to /login", page.url().includes("/login"), page.url());

  const afterLogout = await context.cookies();
  const stillThere = afterLogout.find((c) => c.name === COOKIE_NAME && c.value);
  check("refresh cookie cleared", !stillThere, stillThere ? "value still set" : "gone");

  await page.reload({ waitUntil: "networkidle" });
  await sleep(1200);
  check(
    "reload after logout stays on /login (no zombie session)",
    page.url().includes("/login"),
    page.url(),
  );

  console.log("\n=== console ===");
  if (consoleErrors.length) {
    consoleErrors.forEach((e) => console.log("  " + e));
  } else {
    console.log("  (clean)");
  }
  // A 401 from the startup restoreSession probe is expected and logged by
  // the browser itself; only genuine page errors are disqualifying.
  check("no uncaught page errors", !consoleErrors.some((e) => e.startsWith("PAGEERROR")));

  await browser.close();

  const failed = checks.filter((c) => !c.ok);
  console.log(
    "\n" +
      (failed.length === 0
        ? `PHASE 2.1 SMOKE: ALL ${checks.length} PASS`
        : `PHASE 2.1 SMOKE: ${failed.length} of ${checks.length} FAILED`),
  );
  process.exit(failed.length === 0 ? 0 : 1);
})().catch((e) => {
  console.error("DRIVER ERROR:", e);
  process.exit(2);
});
