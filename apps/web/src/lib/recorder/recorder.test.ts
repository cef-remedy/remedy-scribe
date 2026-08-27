/**
 * Phase 2.2: the recorder's crypto and storage layers.
 *
 * These cover the properties that would fail *silently* if broken — a key
 * that turns out to be extractable, plaintext reaching disk, a chunk query
 * that returns another consultation's audio. All of them keep working
 * perfectly from the user's point of view while being wrong, which is
 * exactly why they get tests rather than a manual check.
 *
 * The genuinely browser-dependent parts (getUserMedia, MediaRecorder,
 * AudioWorklet, wake lock) are covered by the Playwright smoke test against
 * a real Chromium. Mocking MediaRecorder here would only prove the mock
 * works — the same gap that hid a real httpx bug in Phase 1.3.
 */
import { beforeEach, describe, expect, it } from "vitest";

import {
  CHUNK_STORE_NAME,
  KEY_STORE_NAME,
  decryptChunk,
  destroyAudioKey,
  encryptChunk,
  getAudioKey,
  openDb,
} from "./crypto";
import {
  appendChunk,
  assembleSession,
  deleteSession,
  listSessions,
  readSessionChunks,
  type StoredChunk,
} from "./store";

const encoder = new TextEncoder();

function bytes(text: string): ArrayBuffer {
  // Copy into a standalone ArrayBuffer so the type is exact.
  const src = encoder.encode(text);
  const out = new ArrayBuffer(src.byteLength);
  new Uint8Array(out).set(src);
  return out;
}

async function wipeDb(): Promise<void> {
  // Clears the stores rather than deleting the database. deleteDatabase
  // needs exclusive access and silently blocks on any open connection,
  // which made this hook hostage to a connection leak elsewhere — useful
  // as a signal (it found one), useless as a fixture.
  const db = await openDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction([KEY_STORE_NAME, CHUNK_STORE_NAME], "readwrite");
      tx.objectStore(KEY_STORE_NAME).clear();
      tx.objectStore(CHUNK_STORE_NAME).clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

beforeEach(wipeDb);

describe("device key", () => {
  it("is not extractable, which is the whole point", async () => {
    const key = await getAudioKey();

    expect(key.extractable).toBe(false);
    // The property that matters: even holding the key object, the raw bytes
    // cannot be read out. If this ever starts resolving, an XSS payload can
    // exfiltrate the key and "encrypted on device" becomes decorative.
    await expect(crypto.subtle.exportKey("raw", key)).rejects.toThrow();
  });

  it("is stable across calls so earlier chunks stay decryptable", async () => {
    const first = await getAudioKey();
    const second = await getAudioKey();

    const blob = await encryptChunk(first, bytes("consultation audio"));
    // Decrypting with the second handle must work, or a page reload would
    // orphan every chunk written before it.
    await expect(decryptChunk(second, blob)).resolves.toBeInstanceOf(ArrayBuffer);
  });

  it("survives a fresh database connection", async () => {
    const original = await getAudioKey();
    const blob = await encryptChunk(original, bytes("hello"));

    const db = await openDb();
    const reopened = await getAudioKey(db);
    db.close();

    const plaintext = await decryptChunk(reopened, blob);
    expect(new TextDecoder().decode(plaintext)).toBe("hello");
  });

  it("crypto-shreds: destroying the key makes stored audio unrecoverable", async () => {
    const key = await getAudioKey();
    const blob = await encryptChunk(key, bytes("withdrawn consultation"));

    await destroyAudioKey();
    const fresh = await getAudioKey(); // a *new* key, not the old one

    // This is the consent-withdrawal path (P0-1). It must be genuinely
    // unrecoverable, not merely deleted from an index.
    await expect(decryptChunk(fresh, blob)).rejects.toThrow();
  });
});

describe("encryption", () => {
  it("round-trips exactly", async () => {
    const key = await getAudioKey();
    const original = "Ano po ang masakit? Ulo ko po.";

    const blob = await encryptChunk(key, bytes(original));
    const plaintext = await decryptChunk(key, blob);

    expect(new TextDecoder().decode(plaintext)).toBe(original);
  });

  it("produces ciphertext that does not contain the plaintext", async () => {
    const key = await getAudioKey();
    const blob = await encryptChunk(key, bytes("Masakit ang ulo"));

    const asText = new TextDecoder().decode(new Uint8Array(blob.ciphertext));
    expect(asText).not.toContain("Masakit");
    expect(asText).not.toContain("ulo");
  });

  it("uses a fresh IV per chunk", async () => {
    const key = await getAudioKey();
    const a = await encryptChunk(key, bytes("same input"));
    const b = await encryptChunk(key, bytes("same input"));

    // Reusing an IV under the same key breaks GCM catastrophically, so this
    // is worth asserting rather than trusting getRandomValues by inspection.
    expect(Array.from(a.iv)).not.toEqual(Array.from(b.iv));
    // Identical plaintext must not produce identical ciphertext either.
    expect(Array.from(new Uint8Array(a.ciphertext))).not.toEqual(
      Array.from(new Uint8Array(b.ciphertext)),
    );
  });

  it("rejects a tampered chunk rather than decrypting it to garbage", async () => {
    const key = await getAudioKey();
    const blob = await encryptChunk(key, bytes("original audio"));

    const corrupted = new Uint8Array(blob.ciphertext.slice(0));
    corrupted[4] ^= 0xff;

    // GCM authentication doing its job. Garbage that decrypts "successfully"
    // would be transcribed as if it were speech.
    await expect(
      decryptChunk(key, { ciphertext: corrupted.buffer as ArrayBuffer, iv: blob.iv }),
    ).rejects.toThrow();
  });

  it("rejects a truncated chunk", async () => {
    const key = await getAudioKey();
    const blob = await encryptChunk(key, bytes("original audio"));

    await expect(
      decryptChunk(key, { ciphertext: blob.ciphertext.slice(0, 8), iv: blob.iv }),
    ).rejects.toThrow();
  });
});

describe("chunk store", () => {
  async function seed(sessionId: string, count: number, mimeType = "audio/webm;codecs=opus") {
    const key = await getAudioKey();
    for (let seq = 0; seq < count; seq++) {
      const plaintext = bytes(`chunk-${sessionId}-${seq}`);
      const { ciphertext, iv } = await encryptChunk(key, plaintext);
      const chunk: StoredChunk = {
        sessionId,
        seq,
        offsetMs: seq * 5000,
        byteLength: plaintext.byteLength,
        ciphertext,
        iv,
        mimeType,
      };
      await appendChunk(chunk);
    }
    return key;
  }

  it("reads back chunks in sequence order", async () => {
    await seed("session-a", 4);
    const chunks = await readSessionChunks("session-a");

    expect(chunks.map((c) => c.seq)).toEqual([0, 1, 2, 3]);
    expect(chunks.map((c) => c.offsetMs)).toEqual([0, 5000, 10000, 15000]);
  });

  it("never returns another session's audio", async () => {
    await seed("session-a", 3);
    await seed("session-b", 2);

    const a = await readSessionChunks("session-a");
    const b = await readSessionChunks("session-b");

    // A leak here means one patient's consultation appended to another's —
    // a clinical-safety bug, not a data bug.
    expect(a).toHaveLength(3);
    expect(b).toHaveLength(2);
    expect(a.every((c) => c.sessionId === "session-a")).toBe(true);
    expect(b.every((c) => c.sessionId === "session-b")).toBe(true);
  });

  it("stores only ciphertext — no plaintext reaches the database", async () => {
    await seed("session-a", 2);
    const chunks = await readSessionChunks("session-a");

    for (const chunk of chunks) {
      const asText = new TextDecoder().decode(new Uint8Array(chunk.ciphertext));
      expect(asText).not.toContain("chunk-session-a");
    }
  });

  it("survives reconnection, which is what makes it a write-ahead log", async () => {
    await seed("session-a", 3);
    // readSessionChunks opens and closes its own connection, so a second
    // call proves durability across connections rather than in-memory state.
    const again = await readSessionChunks("session-a");
    expect(again).toHaveLength(3);
  });

  it("summarises sessions without decrypting", async () => {
    await seed("session-a", 3);
    await seed("session-b", 1);

    const sessions = await listSessions();
    const byId = Object.fromEntries(sessions.map((s) => [s.sessionId, s]));

    expect(byId["session-a"].chunkCount).toBe(3);
    expect(byId["session-b"].chunkCount).toBe(1);
    expect(byId["session-a"].lastOffsetMs).toBe(10000);
    expect(byId["session-a"].bytes).toBeGreaterThan(0);
  });

  it("deletes one session without touching another", async () => {
    await seed("session-a", 3);
    await seed("session-b", 2);

    const deleted = await deleteSession("session-a");

    expect(deleted).toBe(3);
    expect(await readSessionChunks("session-a")).toHaveLength(0);
    expect(await readSessionChunks("session-b")).toHaveLength(2);
  });

  it("reassembles a session in order", async () => {
    const key = await seed("session-a", 3);
    const { blob, mimeType, chunkCount } = await assembleSession("session-a", (b) =>
      decryptChunk(key, b),
    );

    expect(chunkCount).toBe(3);
    expect(mimeType).toBe("audio/webm;codecs=opus");
    const text = await blob.text();
    // Order matters: out-of-order reassembly produces audio that transcribes
    // into a note with the consultation's events in the wrong sequence.
    expect(text).toBe("chunk-session-a-0chunk-session-a-1chunk-session-a-2");
  });

  it("refuses to reassemble a session with no audio", async () => {
    const key = await getAudioKey();
    await expect(assembleSession("nope", (b) => decryptChunk(key, b))).rejects.toThrow(
      /No stored audio/,
    );
  });

  it("preserves the negotiated container so Safari's MP4 is not mislabelled", async () => {
    const key = await seed("session-mp4", 2, "audio/mp4");
    const { mimeType } = await assembleSession("session-mp4", (b) => decryptChunk(key, b));

    // Hardcoding webm here would hand Groq Whisper a mislabelled file on any
    // Safari before 18.4 (decision 0025).
    expect(mimeType).toBe("audio/mp4");
  });
});
