/**
 * The upload queue's durable store (P0-2, checklist 2.4).
 *
 * This is a **write-ahead log**, and the invariant is the one the checklist
 * names: *the record of intent is committed to durable storage before the
 * risky operation begins.* Concretely, a queue entry — carrying the
 * idempotency key — is written when recording **starts**, not when it
 * finishes and not when the upload begins. So a crash at any point after
 * that leaves enough on disk to reconstruct what should happen.
 *
 * The specific bug this prevents is the one the idempotency key exists for:
 * generate a key in memory, crash before persisting it, retry with a fresh
 * key, and the server sees two unrelated encounters — a duplicate note on
 * one patient. Persisting the key first makes the retry find the original.
 *
 * Lives in the same IndexedDB database as the audio chunks (see
 * recorder/crypto.ts:openDb) at DB_VERSION 2 — one database, one version.
 * Two `open` calls with different upgrade handlers on the same name is a
 * reliable way to get a VersionError in a second tab.
 */
import { QUEUE_STORE_NAME, openDb } from "../recorder/crypto";

/**
 * The state machine. Ordered, and each transition is only made *after* the
 * thing it claims has actually happened.
 *
 *   recording  — capture in progress. The entry exists before any audio does.
 *   pending    — capture finished; audio is on disk, nothing uploaded.
 *   uploading  — parts are going up. Resumable: S3 itself holds part state.
 *   uploaded   — `upload/complete` returned 200. The BYTES are received.
 *   confirmed  — the server's pipeline actually ran (pipeline_status moved
 *                past `uploaded`). Only now may local audio be deleted.
 *   done       — local audio deleted. Terminal, and the only clean end.
 *   failed     — retries exhausted or a permanent rejection. Terminal, and
 *                deliberately does NOT delete local audio: a failed upload
 *                is the case where the laptop holds the only copy.
 *   abandoned  — consent withdrawn. Audio already destroyed; kept as a
 *                record that the entry existed and why it stopped.
 */
export type QueueState =
  | "recording"
  | "pending"
  | "uploading"
  | "uploaded"
  | "confirmed"
  | "done"
  | "failed"
  | "abandoned";

/** States from which the uploader may pick work up. */
export const UPLOADABLE_STATES: QueueState[] = ["pending", "uploading"];
/** States that still hold local audio a doctor could lose. */
export const HOLDS_AUDIO_STATES: QueueState[] = [
  "recording",
  "pending",
  "uploading",
  "uploaded",
  "confirmed",
  "failed",
];

export type QueueEntry = {
  /** Same id as the recorder's `sessionId`, i.e. the encounter id. */
  id: string;
  encounterId: string;
  /**
   * The idempotency key, persisted here so a retry after a crash presents
   * the same one. Assigned at encounter creation and mirrored locally
   * because the server copy is unreachable when offline — which is exactly
   * when a retry is most likely.
   */
  idempotencyKey: string;
  state: QueueState;
  createdAt: number;
  updatedAt: number;
  /** Consecutive failed attempts. Drives the backoff, reset on success. */
  attempts: number;
  /** Wall-clock time before which the uploader must not retry. */
  nextAttemptAt: number;
  /** Last error, shown to the doctor. Never silently swallowed (P0-2). */
  lastError: string | null;
  /** S3 multipart session, so a resumed upload continues rather than restarts. */
  objectKey: string | null;
  uploadId: string | null;
  /** Bytes confirmed accepted by S3, for the progress readout. */
  bytesUploaded: number;
  /** Total plaintext bytes on disk for this recording. */
  bytesTotal: number;
  /** Carried through so the note can be marked as having gaps (see 2.2). */
  audioGapMs: number;
};

export async function putEntry(entry: QueueEntry): Promise<void> {
  const db = await openDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(QUEUE_STORE_NAME, "readwrite");
      tx.objectStore(QUEUE_STORE_NAME).put({ ...entry, updatedAt: Date.now() });
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error ?? new Error("queue write aborted"));
    });
  } finally {
    db.close();
  }
}

export async function getEntry(id: string): Promise<QueueEntry | null> {
  const db = await openDb();
  try {
    return await new Promise<QueueEntry | null>((resolve, reject) => {
      const request = db.transaction(QUEUE_STORE_NAME, "readonly").objectStore(QUEUE_STORE_NAME).get(id);
      request.onsuccess = () => resolve((request.result as QueueEntry | undefined) ?? null);
      request.onerror = () => reject(request.error);
    });
  } finally {
    db.close();
  }
}

export async function listEntries(): Promise<QueueEntry[]> {
  const db = await openDb();
  try {
    const all = await new Promise<QueueEntry[]>((resolve, reject) => {
      const request = db.transaction(QUEUE_STORE_NAME, "readonly").objectStore(QUEUE_STORE_NAME).getAll();
      request.onsuccess = () => resolve(request.result as QueueEntry[]);
      request.onerror = () => reject(request.error);
    });
    return all.sort((a, b) => a.createdAt - b.createdAt);
  } finally {
    db.close();
  }
}

export async function deleteEntry(id: string): Promise<void> {
  const db = await openDb();
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(QUEUE_STORE_NAME, "readwrite");
      tx.objectStore(QUEUE_STORE_NAME).delete(id);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

/**
 * Marks an entry's state, patching only what changed.
 *
 * Read-modify-write rather than a blind put: two callers can touch the same
 * entry (the uploader advancing state while the UI records a gap count), and
 * a blind put would silently drop the other's field.
 */
export async function patchEntry(id: string, patch: Partial<QueueEntry>): Promise<QueueEntry | null> {
  const existing = await getEntry(id);
  if (!existing) return null;
  const merged = { ...existing, ...patch, updatedAt: Date.now() };
  await putEntry(merged);
  return merged;
}

/* ------------------------------------------------------------------ *
 * Backoff
 * ------------------------------------------------------------------ */

/** Roughly 5s, 10s, 20s, 40s, … capped at 5 minutes. */
export const BASE_BACKOFF_MS = 5_000;
export const MAX_BACKOFF_MS = 5 * 60_000;
/**
 * After this many consecutive failures the entry is marked `failed` and
 * surfaced to the doctor rather than retried forever. Retrying a permanently
 * broken upload silently is the "fails silently" outcome P0-2 forbids.
 */
export const MAX_ATTEMPTS = 8;

export function backoffMs(attempts: number): number {
  const exponential = BASE_BACKOFF_MS * 2 ** Math.max(0, attempts - 1);
  const capped = Math.min(exponential, MAX_BACKOFF_MS);
  // Jitter: many queued recordings retrying in lockstep after a wifi outage
  // would hammer the API in synchronised waves.
  const jitter = capped * 0.2 * Math.random();
  return Math.round(capped - capped * 0.1 + jitter);
}
