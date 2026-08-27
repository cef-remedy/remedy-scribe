/**
 * The write-ahead chunk store.
 *
 * Checklist 2.2: "write audio in chunks as you go, never buffering a whole
 * consult in memory." Checklist 2.4's Understand-first note names the
 * pattern: this is a write-ahead log, and the invariant is that the record
 * of intent reaches durable storage *before* the risky operation.
 *
 * Two granularities that must not be confused:
 *
 *   - **Recorder chunk** — ~5s of audio, ~20 KB at mono Opus 32 kbps
 *     (decision 0025). Small on purpose: a crash or a lid-close loses at
 *     most one chunk instead of the consultation.
 *   - **S3 upload part** — minimum 5 MB (`MIN_PART_SIZE_BYTES` in
 *     app/services/storage.py), except the final part. At 32 kbps that is
 *     roughly **21 minutes of audio per part**, so a typical consult is one
 *     or two parts. Phase 2.4 assembles many chunks into each part; they
 *     are emphatically not 1:1, and sizing chunks to the S3 minimum would
 *     mean risking 21 minutes of audio per crash.
 *
 * Every stored chunk is already ciphertext (see crypto.ts) — plaintext
 * audio never reaches disk.
 */
import { CHUNK_STORE_NAME, openDb, type EncryptedBlob } from "./crypto";

export type StoredChunk = {
  id?: number;
  sessionId: string;
  /** Monotonic within a session; the reassembly order. */
  seq: number;
  /** Milliseconds since the recording's own start, for gap reporting. */
  offsetMs: number;
  /** Plaintext byte length, kept so size can be reported without decrypting. */
  byteLength: number;
  ciphertext: ArrayBuffer;
  iv: Uint8Array<ArrayBuffer>;
  /** Container the recorder actually negotiated, e.g. "audio/webm;codecs=opus". */
  mimeType: string;
};

export async function appendChunk(chunk: StoredChunk): Promise<number> {
  const db = await openDb();
  try {
    return await new Promise<number>((resolve, reject) => {
      const tx = db.transaction(CHUNK_STORE_NAME, "readwrite");
      const request = tx.objectStore(CHUNK_STORE_NAME).add(chunk);
      request.onsuccess = () => resolve(request.result as number);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error ?? new Error("chunk write aborted"));
    });
  } finally {
    db.close();
  }
}

/**
 * All chunks for a session, in sequence order. Survives a reload and a
 * crash, which is the entire point: after the browser dies mid-consult this
 * is what is left, and it must be enough to reconstruct the audio.
 */
export async function readSessionChunks(sessionId: string): Promise<StoredChunk[]> {
  const db = await openDb();
  try {
    const chunks = await new Promise<StoredChunk[]>((resolve, reject) => {
      const index = db
        .transaction(CHUNK_STORE_NAME, "readonly")
        .objectStore(CHUNK_STORE_NAME)
        .index("bySession");
      // Bounded to this session only. A plain getAll() would pull every
      // queued consultation into memory, which is the thing this module
      // exists to avoid.
      const range = IDBKeyRange.bound([sessionId, -Infinity], [sessionId, Infinity]);
      const request = index.getAll(range);
      request.onsuccess = () => resolve(request.result as StoredChunk[]);
      request.onerror = () => reject(request.error);
    });
    return chunks.sort((a, b) => a.seq - b.seq);
  } finally {
    db.close();
  }
}

export async function listSessions(): Promise<
  { sessionId: string; chunkCount: number; bytes: number; lastOffsetMs: number }[]
> {
  const db = await openDb();
  try {
    const all = await new Promise<StoredChunk[]>((resolve, reject) => {
      const request = db
        .transaction(CHUNK_STORE_NAME, "readonly")
        .objectStore(CHUNK_STORE_NAME)
        .getAll();
      request.onsuccess = () => resolve(request.result as StoredChunk[]);
      request.onerror = () => reject(request.error);
    });
    const bySession = new Map<string, { chunkCount: number; bytes: number; lastOffsetMs: number }>();
    for (const chunk of all) {
      const entry = bySession.get(chunk.sessionId) ?? { chunkCount: 0, bytes: 0, lastOffsetMs: 0 };
      entry.chunkCount += 1;
      entry.bytes += chunk.byteLength;
      entry.lastOffsetMs = Math.max(entry.lastOffsetMs, chunk.offsetMs);
      bySession.set(chunk.sessionId, entry);
    }
    return [...bySession.entries()].map(([sessionId, v]) => ({ sessionId, ...v }));
  } finally {
    db.close();
  }
}

/**
 * Deletes one session's chunks. The per-encounter deletion path — used
 * after a successful upload (P0-2: local audio deleted only once the server
 * confirms receipt *and* pipeline start) and for a consent withdrawal
 * scoped to a single encounter.
 */
export async function deleteSession(sessionId: string): Promise<number> {
  const chunks = await readSessionChunks(sessionId);
  const db = await openDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(CHUNK_STORE_NAME, "readwrite");
      const store = tx.objectStore(CHUNK_STORE_NAME);
      for (const chunk of chunks) {
        if (chunk.id !== undefined) store.delete(chunk.id);
      }
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
    return chunks.length;
  } finally {
    db.close();
  }
}

/**
 * Reassembles a session's plaintext audio into one Blob.
 *
 * Deliberately NOT used during recording — it is the upload/playback path
 * (Phase 2.4), and it is the one place a whole consult is held in memory.
 * At mono Opus 32 kbps a 30-minute consult is ~7 MB, so that is acceptable
 * here in a way it would not be at the 129 kbps the harness accidentally
 * produced (~27 MB). Worth remembering if the bitrate ever rises.
 */
export async function assembleSession(
  sessionId: string,
  decrypt: (blob: EncryptedBlob) => Promise<ArrayBuffer>,
): Promise<{ blob: Blob; mimeType: string; chunkCount: number }> {
  const chunks = await readSessionChunks(sessionId);
  if (chunks.length === 0) {
    throw new Error(`No stored audio for session ${sessionId}`);
  }

  const parts: ArrayBuffer[] = [];
  for (const chunk of chunks) {
    // Sequential, not Promise.all: a 30-minute consult is ~360 chunks, and
    // decrypting them all concurrently would spike memory for no gain.
    parts.push(await decrypt({ ciphertext: chunk.ciphertext, iv: chunk.iv }));
  }

  const mimeType = chunks[0].mimeType;
  return { blob: new Blob(parts, { type: mimeType }), mimeType, chunkCount: chunks.length };
}
