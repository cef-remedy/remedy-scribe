/**
 * Self-test for audio-capture-harness.html.
 *
 * The harness is a measurement instrument, so it needs its own
 * calibration check — an instrument that silently reads wrong is worse
 * than no instrument. This drives it in Chromium against a synthetic
 * microphone and asserts the measurement machinery works: worklet loads,
 * the audio clock advances, drift lands near zero on a healthy run,
 * MediaRecorder chunks arrive, IndexedDB persists, and the verdict
 * renders. It also backgrounds the tab for real (second tab to front) to
 * exercise the throttle path.
 *
 * It caught two real bugs on first run: drift was being measured from
 * the click rather than from when the audio graph started delivering
 * (charging ~1.2s of startup latency to "audio loss"), and the 250ms
 * worklet post interval added ~1% of quantization — together enough to
 * fire a false "audio loss" warning on a perfectly healthy baseline.
 *
 * Run:
 *   # 1. serve the repo (getUserMedia needs a secure context; localhost counts)
 *   python -m http.server 8765
 *
 *   # 2. drive it (needs playwright + its chromium available)
 *   PW_PATH=/path/to/node_modules/playwright \
 *   HARNESS_URL=http://localhost:8765/docs/experiments/audio-capture-harness.html \
 *   node docs/experiments/verify-harness.js
 *
 * Exit 0 = all checks pass. This validates the HARNESS, not the clinic
 * hardware — the real question (does capture survive lid close on the
 * actual laptop?) can only be answered by a human running the page.
 */
const PW_PATH = process.env.PW_PATH || "playwright";
const HARNESS_URL =
  process.env.HARNESS_URL ||
  "http://localhost:8765/docs/experiments/audio-capture-harness.html";

const { chromium } = require(PW_PATH);

const FOREGROUND_MS = 12000;
const BACKGROUND_MS = 12000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({
    // Headed: fake media streams and AudioWorklet behave more faithfully,
    // and tab backgrounding via bringToFront only really works here.
    headless: false,
    args: [
      "--use-fake-device-for-media-stream",
      "--use-fake-ui-for-media-stream",
      "--autoplay-policy=no-user-gesture-required",
    ],
  });
  const context = await browser.newContext({ permissions: ["microphone"] });
  const page = await context.newPage();

  const consoleErrors = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  page.on("pageerror", (e) => consoleErrors.push("PAGEERROR: " + e.message));

  await page.goto(HARNESS_URL, { waitUntil: "load" });

  const secure = await page.evaluate(() => window.isSecureContext);
  console.log("secureContext:", secure);

  const envRows = await page.evaluate(() =>
    [...document.querySelectorAll("#env tr")]
      .slice(1)
      .map((tr) => tr.children[0].textContent + " = " + tr.children[1].textContent)
  );
  console.log("\n--- ENVIRONMENT ---");
  envRows.forEach((r) => console.log("  " + r));

  console.log("\n--- STARTING RUN ---");
  await page.click("#start");
  await sleep(FOREGROUND_MS);
  console.log("\nafter " + FOREGROUND_MS / 1000 + "s foreground:");
  dump(await readMetrics(page));

  console.log("\n--- BACKGROUNDING TAB (second tab to front) ---");
  const other = await context.newPage();
  await other.goto("about:blank");
  await other.bringToFront();
  await sleep(BACKGROUND_MS);
  await page.bringToFront();
  await sleep(1500);
  console.log("\nafter " + BACKGROUND_MS / 1000 + "s backgrounded + refocus:");
  dump(await readMetrics(page));

  await page.click("#stop");
  await sleep(1200);

  const final = await readMetrics(page);
  console.log("\n--- FINAL ---");
  dump(final);

  const verdicts = await page.evaluate(() =>
    [...document.querySelectorAll("#verdicts .verdict")].map(
      (d) =>
        "[" + d.className.replace("verdict ", "") + "] " + d.querySelector("b").textContent
    )
  );
  console.log("\n--- VERDICTS ---");
  verdicts.forEach((v) => console.log("  " + v));

  const stored = await page.evaluate(() => document.getElementById("stored").textContent);
  console.log("\n--- INDEXEDDB ---\n  " + stored.replace(/\n/g, "\n  "));

  const logTail = await page.evaluate(() =>
    document.getElementById("log").textContent.split("\n").slice(0, 20).join("\n")
  );
  console.log("\n--- EVENT LOG (newest first) ---\n" + logTail);

  if (consoleErrors.length) {
    console.log("\n--- CONSOLE ERRORS ---");
    consoleErrors.forEach((e) => console.log("  " + e));
  }

  console.log("\n--- HARNESS SELF-CHECK ---");
  const checks = [
    ["secure context", secure === true],
    ["audio clock advanced", final.audioSec > 20],
    // The point of the whole instrument: a healthy synthetic run must
    // read as no loss. Anything above 0.5% here means the measurement
    // is miscalibrated, not that audio was lost.
    ["drift near zero on a healthy run", final.driftPct !== null && Math.abs(final.driftPct) < 0.5],
    ["startup latency reported separately", final.startupNoted],
    ["worker ticks accumulated", final.workerTicks > 15],
    ["page ticks accumulated", final.pageTicks > 5],
    ["MediaRecorder produced chunks", final.chunks >= 3],
    ["bytes captured", final.bytesKB > 0],
    ["worklet never stalled", final.maxMsgGapMs !== null && final.maxMsgGapMs < 1500],
    ["IndexedDB persisted a run", /run \d+:/.test(stored)],
    ["verdict says no audio loss", verdicts.some((v) => /No audio loss/.test(v))],
    ["no console errors", consoleErrors.length === 0],
  ];

  let pass = true;
  for (const [name, ok] of checks) {
    console.log("  " + (ok ? "PASS" : "FAIL") + "  " + name);
    if (!ok) pass = false;
  }

  await browser.close();
  console.log("\n" + (pass ? "HARNESS SELF-CHECK: ALL PASS" : "HARNESS SELF-CHECK: FAILURES ABOVE"));
  process.exit(pass ? 0 : 1);
})().catch((e) => {
  console.error("DRIVER ERROR:", e);
  process.exit(2);
});

function readMetrics(page) {
  return page.evaluate(() => {
    const val = (id) => document.querySelector("#" + id + " .val").textContent;
    const note = (id) => document.querySelector("#" + id + " .note").textContent;
    const num = (s) => {
      const m = String(s).match(/-?[\d.]+/);
      return m ? parseFloat(m[0]) : null;
    };
    const ticks = (id) => {
      const p = val(id).split("/").map((x) => parseInt(x.trim(), 10));
      return { got: p[0], exp: p[1] };
    };
    // "worst 250ms / ctx 24.3s"
    const liveNote = note("m-ctx");
    const gapMatch = liveNote.match(/worst\s+(\d+)ms/);
    return {
      wallSec: num(val("m-wall")),
      audioSec: num(val("m-audio")),
      driftPct: num(val("m-drift")),
      livenessMs: num(val("m-ctx")),
      maxMsgGapMs: gapMatch ? parseInt(gapMatch[1], 10) : null,
      startupNoted: /startup/.test(note("m-wall")),
      pageTicks: ticks("m-page").got,
      pageExp: ticks("m-page").exp,
      workerTicks: ticks("m-worker").got,
      workerExp: ticks("m-worker").exp,
      chunks: parseInt(val("m-chunks"), 10) || 0,
      bytesKB: num(note("m-chunks")) || 0,
      worstGap: val("m-gap"),
      sleep: val("m-sleep"),
      state: val("m-state"),
    };
  });
}

function dump(m) {
  console.log(
    "  wall=" + m.wallSec + "s  audio=" + m.audioSec + "s  drift=" + m.driftPct + "%"
  );
  console.log(
    "  liveness=" + m.livenessMs + "ms  worstWorkletGap=" + m.maxMsgGapMs + "ms" +
    "  startupReported=" + m.startupNoted
  );
  console.log(
    "  pageTicks=" + m.pageTicks + "/" + m.pageExp +
    "  workerTicks=" + m.workerTicks + "/" + m.workerExp
  );
  console.log(
    "  chunks=" + m.chunks + " (" + m.bytesKB + " KB)  worstChunkGap=" + m.worstGap +
    "  sleep=" + m.sleep + "  track=" + m.state
  );
}
