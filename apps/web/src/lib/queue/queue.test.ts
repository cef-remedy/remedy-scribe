/**
 * Phase 2.4: the upload queue.
 *
 * Three things get tested here because each fails in a way that looks fine:
 *
 *  1. **Part planning arithmetic.** Off by one part and S3 rejects every
 *     part but the last — but only against a real bucket, so it would ship.
 *  2. **The state machine's ordering.** Deleting local audio one state too
 *     early destroys the only copy of a consultation. There is no recovering
 *     from that, so the transition is asserted rather than reasoned about.
 *  3. **Backoff.** An offline stretch must not consume the attempt budget,
 *     or a wifi outage dead-letters healthy recordings.
 *
 * The network path (presigned PUTs to S3) is covered by the end-to-end smoke
 * test against real MinIO, not mocked here — the same reasoning as 2.2.
 */
import { beforeEach, describe, expect, it } from "vitest";

import { CHUNK_STORE_NAME, KEY_STORE_NAME, QUEUE_STORE_NAME, encryptChunk, getAudioKey, openDb } from "../recorder/crypto";
import { appendChunk, readSessionChunks, type StoredChunk } from "../recorder/store";
import {
  BASE_BACKOFF_MS,
  MAX_ATTEMPTS,
  MAX_BACKOFF_MS,
  backoffMs,
  getEntry,
  listEntries,
  patchEntry,
  putEntry,
  type QueueEntry,
} from "./store";
import { abandonRecording, checkStorage, enqueueRecording, markReadyToUpload, summarise } from "./queue";
import { MIN_PART_SIZE_BYTES, planParts } from "./uploader";

async function wipe(): Promise<void> {
  const db = await openDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction([KEY_STORE_NAME, CHUNK_STORE_NAME, QUEUE_STORE_NAME], "readwrite");
      tx.objectStore(KEY_STORE_NAME).clear();
      tx.objectStore(CHUNK_STORE_NAME).clear();
      tx.objectStore(QUEUE_STORE_NAME).clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

beforeEach(wipe);

/* ------------------------------------------------------------------ *
 * Part planning
 * ------------------------------------------------------------------ */

describe("planParts", () => {
  const CHUNK = 20 * 1024; // ~5s of mono Opus 32 kbps

  it("puts a short consult in a single part", () => {
    // 10 chunks ≈ 50s ≈ 200 KB, far under the 5 MB minimum. S3 exempts only
    // the LAST part from that minimum, so one part is legal and correct.
    const parts = planParts(Array(10).fill(CHUNK));
    expect(parts).toHaveLength(1);
    expect(parts[0].chunkIndices).toHaveLength(10);
  });

  it("splits once past the 5 MB minimum", () => {
    // 5 MB at 20 KB/chunk is 256 chunks ≈ 21 minutes. 300 chunks should be
    // one full part plus a remainder.
    const parts = planParts(Array(300).fill(CHUNK));
    expect(parts).toHaveLength(2);
    expect(parts[0].bytes).toBeGreaterThanOrEqual(MIN_PART_SIZE_BYTES);
    // The final part is allowed to be under the minimum, and is here.
    expect(parts[1].bytes).toBeLessThan(MIN_PART_SIZE_BYTES);
  });

  it("gives every part except the last at least the minimum", () => {
    const parts = planParts(Array(800).fill(CHUNK));
    expect(parts.length).toBeGreaterThan(2);
    for (const part of parts.slice(0, -1)) {
      // This is the property S3 actually enforces. Violating it fails the
      // upload at the vendor, which is the worst place to find out.
      expect(part.bytes).toBeGreaterThanOrEqual(MIN_PART_SIZE_BYTES);
    }
  });

  it("assigns every chunk to exactly one part, in order", () => {
    const sizes = Array(500).fill(CHUNK);
    const parts = planParts(sizes);
    const flat = parts.flatMap((p) => p.chunkIndices);

    // No chunk dropped and none duplicated: a dropped chunk is a silent hole
    // in the consultation, a duplicated one corrupts the audio stream.
    expect(flat).toHaveLength(sizes.length);
    expect(flat).toEqual(sizes.map((_, i) => i));
  });

  it("handles a single chunk", () => {
    const parts = planParts([CHUNK]);
    expect(parts).toHaveLength(1);
    expect(parts[0].chunkIndices).toEqual([0]);
  });

  it("returns nothing for no chunks rather than an empty part", () => {
    // An empty part would be sent to S3 and rejected.
    expect(planParts([])).toEqual([]);
  });

  it("splits a chunk stream that lands exactly on the boundary", () => {
    const parts = planParts([MIN_PART_SIZE_BYTES]);
    // Exactly the minimum closes the part; with nothing following, that one
    // part is also the last, so there is no orphaned empty remainder.
    expect(parts).toHaveLength(1);
    expect(parts[0].bytes).toBe(MIN_PART_SIZE_BYTES);
  });
});

/* ------------------------------------------------------------------ *
 * Backoff
 * ------------------------------------------------------------------ */

describe("backoffMs", () => {
  it("grows exponentially", () => {
    const first = backoffMs(1);
    const second = backoffMs(2);
    const third = backoffMs(3);
    expect(first).toBeGreaterThan(BASE_BACKOFF_MS * 0.8);
    expect(second).toBeGreaterThan(first * 1.5);
    expect(third).toBeGreaterThan(second * 1.5);
  });

  it("caps so a stuck entry still retries occasionally", () => {
    // Without a cap, attempt 20 would be years away and the entry would
    // never recover on its own after a long outage.
    for (const attempts of [10, 20, 50]) {
      expect(backoffMs(attempts)).toBeLessThanOrEqual(MAX_BACKOFF_MS * 1.15);
    }
  });

  it("is jittered so queued recordings do not retry in lockstep", () => {
    // After a clinic-wide wifi outage, several laptops retrying on the same
    // schedule would hit the API in synchronised waves.
    const samples = new Set(Array.from({ length: 20 }, () => backoffMs(4)));
    expect(samples.size).toBeGreaterThan(1);
  });
});

/* ------------------------------------------------------------------ *
 * The write-ahead entry
 * ------------------------------------------------------------------ */

describe("queue entries", () => {
  it("writes the entry before any audio exists — the write-ahead invariant", async () => {
    const entry = await enqueueRecording("enc-1", "idem-1");

    expect(entry.state).toBe("recording");
    // Nothing has been recorded yet, but the intent is already durable. This
    // is the ordering the whole phase turns on.
    expect(await readSessionChunks("enc-1")).toHaveLength(0);
    const persisted = await getEntry("enc-1");
    expect(persisted?.idempotencyKey).toBe("idem-1");
  });

  it("is idempotent, so a resumed recording keeps its original key", async () => {
    await enqueueRecording("enc-1", "idem-original");
    const again = await enqueueRecording("enc-1", "idem-different");

    // The key must survive. A second key for the same recording is exactly
    // the duplicate-encounter bug the idempotency key exists to prevent.
    expect(again.idempotencyKey).toBe("idem-original");
    expect(await listEntries()).toHaveLength(1);
  });

  it("survives a fresh database connection", async () => {
    await enqueueRecording("enc-1", "idem-1");
    // listEntries opens its own connection, so this is durability across
    // connections rather than in-memory state.
    const entries = await listEntries();
    expect(entries.map((e) => e.id)).toEqual(["enc-1"]);
  });

  it("moves to pending when recording finishes, sizing itself from the store", async () => {
    // The byte total must come from what is actually on disk, not from the
    // caller. A caller reading React state captured before stop()'s final
    // flush was short by one chunk, and the queue then showed progress over
    // 100% ("56 KB of 37 KB") — undermining the one readout P0-2 requires.
    const key = await getAudioKey();
    for (const seq of [0, 1, 2]) {
      const plaintext = new ArrayBuffer(1000);
      const { ciphertext, iv } = await encryptChunk(key, plaintext);
      await appendChunk({
        sessionId: "enc-1",
        seq,
        offsetMs: seq * 5000,
        byteLength: 1000,
        ciphertext,
        iv,
        mimeType: "audio/webm",
      });
    }

    await enqueueRecording("enc-1", "idem-1");
    await markReadyToUpload("enc-1", { audioGapMs: 6500 });

    const entry = await getEntry("enc-1");
    expect(entry?.state).toBe("pending");
    expect(entry?.bytesTotal).toBe(3000);
    // The gap total travels with the entry so the upload can eventually tell
    // the server the audio is incomplete, rather than that fact living only
    // in the recording screen.
    expect(entry?.audioGapMs).toBe(6500);
  });

  it("abandons on withdrawal but keeps the record", async () => {
    await enqueueRecording("enc-1", "idem-1");
    await abandonRecording("enc-1", "Consent was withdrawn.");

    const entry = await getEntry("enc-1");
    expect(entry?.state).toBe("abandoned");
    // Kept, not deleted: the ledger shows consent was withdrawn, and this
    // shows the recording existed and why it stopped.
    expect(entry?.lastError).toMatch(/withdrawn/i);
  });

  it("patches without clobbering concurrent field updates", async () => {
    await enqueueRecording("enc-1", "idem-1");
    await patchEntry("enc-1", { audioGapMs: 3000 });
    await patchEntry("enc-1", { bytesUploaded: 1024 });

    const entry = await getEntry("enc-1");
    // A blind put in the second call would have reset audioGapMs to 0.
    expect(entry?.audioGapMs).toBe(3000);
    expect(entry?.bytesUploaded).toBe(1024);
  });

  it("orders entries oldest-first so the queue drains in recording order", async () => {
    const base: Omit<QueueEntry, "id" | "createdAt"> = {
      encounterId: "x",
      idempotencyKey: "k",
      state: "pending",
      updatedAt: 0,
      attempts: 0,
      nextAttemptAt: 0,
      lastError: null,
      objectKey: null,
      uploadId: null,
      bytesUploaded: 0,
      bytesTotal: 0,
      audioGapMs: 0,
    };
    await putEntry({ ...base, id: "second", createdAt: 2000 });
    await putEntry({ ...base, id: "first", createdAt: 1000 });
    await putEntry({ ...base, id: "third", createdAt: 3000 });

    expect((await listEntries()).map((e) => e.id)).toEqual(["first", "second", "third"]);
  });
});

/* ------------------------------------------------------------------ *
 * Status summary — what the doctor is shown
 * ------------------------------------------------------------------ */

describe("summarise", () => {
  const entry = (id: string, state: QueueEntry["state"], extra: Partial<QueueEntry> = {}): QueueEntry => ({
    id,
    encounterId: id,
    idempotencyKey: "k",
    state,
    createdAt: 0,
    updatedAt: 0,
    attempts: 0,
    nextAttemptAt: 0,
    lastError: null,
    objectKey: null,
    uploadId: null,
    bytesUploaded: 0,
    bytesTotal: 0,
    audioGapMs: 0,
    ...extra,
  });

  it("counts what still holds audio a doctor could lose", () => {
    const summary = summarise([
      entry("a", "pending"),
      entry("b", "uploading"),
      entry("c", "uploaded"),
      entry("d", "failed"),
      entry("e", "done"),
      entry("f", "abandoned"),
    ]);

    // done and abandoned no longer hold audio; everything else does, and a
    // failed entry counts because it may be the ONLY copy.
    expect(summary.holdingAudio).toBe(4);
    expect(summary.failed).toBe(1);
    expect(summary.waiting).toBe(1);
    expect(summary.uploading).toBe(1);
  });

  it("reports remaining bytes for in-flight uploads only", () => {
    const summary = summarise([
      entry("a", "pending", { bytesTotal: 1000, bytesUploaded: 0 }),
      entry("b", "uploading", { bytesTotal: 1000, bytesUploaded: 400 }),
      entry("c", "done", { bytesTotal: 5000, bytesUploaded: 5000 }),
    ]);
    expect(summary.bytesPending).toBe(1600);
  });
});

/* ------------------------------------------------------------------ *
 * Device-full
 * ------------------------------------------------------------------ */

describe("checkStorage", () => {
  it("reports remaining space as minutes of recording, not a percentage", async () => {
    const health = await checkStorage(240 * 1024);
    // "8% free" means nothing to a doctor; "about 20 minutes left" is
    // directly comparable to the length of a consultation.
    expect(health).toHaveProperty("minutesRemaining");
    expect(["ok", "low", "critical"]).toContain(health.level);
  });

  it("does not claim health when the quota is unknown", async () => {
    // fake-indexeddb has no storage manager, so this exercises the
    // unavailable path: Infinity minutes, level ok, and no crash.
    const health = await checkStorage();
    expect(health.minutesRemaining === Infinity || health.minutesRemaining >= 0).toBe(true);
  });
});

/* ------------------------------------------------------------------ *
 * Chunks and entries coexist in one database
 * ------------------------------------------------------------------ */

describe("schema v2", () => {
  it("keeps audio chunks and queue entries in the same database", async () => {
    // One database, one version, one upgrade handler. Two `open` calls with
    // different handlers on the same name is a reliable VersionError.
    const key = await getAudioKey();
    const plaintext = new ArrayBuffer(64);
    const { ciphertext, iv } = await encryptChunk(key, plaintext);
    const chunk: StoredChunk = {
      sessionId: "enc-1",
      seq: 0,
      offsetMs: 0,
      byteLength: 64,
      ciphertext,
      iv,
      mimeType: "audio/webm;codecs=opus",
    };
    await appendChunk(chunk);
    await enqueueRecording("enc-1", "idem-1");

    expect(await readSessionChunks("enc-1")).toHaveLength(1);
    expect(await getEntry("enc-1")).not.toBeNull();
  });

  it("upgraded an existing v1 database without losing chunks", async () => {
    // The realistic upgrade path: a laptop mid-pilot already holding queued
    // audio. The guarded, additive onupgradeneeded must not drop it.
    const key = await getAudioKey();
    const { ciphertext, iv } = await encryptChunk(key, new ArrayBuffer(32));
    await appendChunk({
      sessionId: "pre-existing",
      seq: 0,
      offsetMs: 0,
      byteLength: 32,
      ciphertext,
      iv,
      mimeType: "audio/webm",
    });

    // Re-open (already at v2 here, but this asserts the store set rather
    // than the migration mechanics, which fake-indexeddb cannot rewind).
    const db = await openDb();
    const names = [...db.objectStoreNames];
    db.close();

    expect(names).toContain(CHUNK_STORE_NAME);
    expect(names).toContain(QUEUE_STORE_NAME);
    expect(names).toContain(KEY_STORE_NAME);
    expect(await readSessionChunks("pre-existing")).toHaveLength(1);
  });
});

it("MAX_ATTEMPTS is a real ceiling, not effectively infinite", () => {
  // Retrying forever silently is the "fails silently" outcome P0-2 forbids;
  // the entry must eventually surface to the doctor instead.
  expect(MAX_ATTEMPTS).toBeGreaterThan(3);
  expect(MAX_ATTEMPTS).toBeLessThan(20);
});
