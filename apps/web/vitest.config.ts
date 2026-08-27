import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // node, not a DOM environment: these suites cover the crypto and
    // storage logic, which uses IndexedDB and WebCrypto — both available in
    // Node (via fake-indexeddb and node:crypto.webcrypto) with no DOM.
    // The parts that genuinely need a browser (getUserMedia, MediaRecorder,
    // AudioWorklet, wake lock) are covered by the Playwright smoke test
    // against a real Chromium instead, because a jsdom mock of MediaRecorder
    // would only ever prove the mock works — the exact gap that hid a real
    // httpx bug in Phase 1.3.
    environment: "node",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.ts"],
  },
});
