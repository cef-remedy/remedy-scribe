/**
 * The queue runner (P0-2, checklist 2.4).
 *
 * A single loop that walks the durable queue, uploads what is due, and
 * deletes local audio only once the server's pipeline has actually started.
 * Everything it needs to resume after an app kill or a laptop restart is in
 * IndexedDB — nothing important lives in this module's memory.
 */
import { OfflineError } from "../../api/client";
import { deleteSession, readSessionChunks } from "../recorder/store";
import {
  MAX_ATTEMPTS,
  backoffMs,
  getEntry,
  listEntries,
  patchEntry,
  putEntry,
  type QueueEntry,
  type QueueState,
} from "./store";
import { PermanentUploadError, pipelineHasStarted, uploadSession } from "./uploader";

/**
 * Written when recording **starts**, before any audio exists — the
 * write-ahead invariant. A crash mid-recording then leaves both the chunks
 * and the intent on disk, and the queue picks the recording up on next
 * launch instead of orphaning it.
 */
export async function enqueueRecording(
  encounterId: string,
  idempotencyKey: string,
): Promise<QueueEntry> {
  const existing = await getEntry(encounterId);
  if (existing) return existing; // idempotent: a resumed recording keeps its entry

  const now = Date.now();
  const entry: QueueEntry = {
    id: encounterId,
    encounterId,
    idempotencyKey,
    state: "recording",
    createdAt: now,
    updatedAt: now,
    attempts: 0,
    nextAttemptAt: 0,
    lastError: null,
    objectKey: null,
    uploadId: null,
    bytesUploaded: 0,
    bytesTotal: 0,
    audioGapMs: 0,
  };
  await putEntry(entry);
  return entry;
}

/**
 * Recording finished: the entry becomes eligible for upload.
 *
 * `bytesTotal` is read from the chunk store rather than accepted from the
 * caller. The caller's number came from React state captured before
 * `stop()` flushed MediaRecorder's final chunk, so it was short by exactly
 * one chunk — and the queue then displayed "56 KB of 37 KB", progress over
 * 100%, which undermines the one status readout P0-2 actually requires. The
 * store is the only thing that knows what is really on disk.
 */
export async function markReadyToUpload(
  encounterId: string,
  info: { audioGapMs: number },
): Promise<void> {
  const chunks = await readSessionChunks(encounterId);
  const bytesTotal = chunks.reduce((n, c) => n + c.byteLength, 0);
  await patchEntry(encounterId, {
    state: "pending",
    bytesTotal,
    audioGapMs: info.audioGapMs,
    nextAttemptAt: 0,
    // Cleared explicitly: a stale "recording was interrupted" note from a
    // heartbeat gap would otherwise survive a perfectly normal stop and be
    // shown to the doctor as a fault.
    lastError: null,
  });
}

/**
 * How long an entry may sit in `recording` without a heartbeat before the
 * queue treats it as interrupted. Comfortably longer than the ~5s chunk
 * interval so an ordinary pause between writes never trips it.
 */
export const RECORDING_STALE_MS = 30_000;

/**
 * Heartbeat from a live recording.
 *
 * Without this, `recoverInterrupted` cannot tell a crashed recording from
 * one happening right now in this very tab — and it got that wrong: a
 * 14-second recording was labelled "interrupted" and pushed into the upload
 * queue *while still capturing*, which both showed the doctor a false fault
 * and risked uploading a recording before its last chunks existed.
 *
 * A timestamp is the right signal rather than an in-memory flag, because it
 * survives the process dying — which is exactly the case being detected.
 */
export async function markRecordingAlive(encounterId: string): Promise<void> {
  // patchEntry stamps updatedAt on every write, so an empty patch is a
  // heartbeat.
  await patchEntry(encounterId, {});
}

/** Consent withdrawn: the audio is already destroyed, keep the record. */
export async function abandonRecording(encounterId: string, reason: string): Promise<void> {
  await patchEntry(encounterId, { state: "abandoned", lastError: reason });
}

/**
 * A recording interrupted by a crash. On launch its entry is still
 * `recording`, but nothing is capturing any more — so it is promoted to
 * `pending` and uploaded. Partial audio from a crashed consultation is far
 * better than none, which is the entire reason chunks are written as they go.
 */
async function recoverInterrupted(entries: QueueEntry[]): Promise<QueueEntry[]> {
  const recovered: QueueEntry[] = [];
  const now = Date.now();
  for (const entry of entries) {
    if (entry.state !== "recording") continue;
    // A live recording heartbeats (see markRecordingAlive). Only a stale one
    // is genuinely interrupted. Skipping this check caused a normally-stopped
    // 14-second recording to be labelled "interrupted" and queued for upload
    // mid-capture.
    if (now - entry.updatedAt < RECORDING_STALE_MS) continue;

    const chunks = await readSessionChunks(entry.id);
    if (chunks.length === 0) {
      // Never captured anything — an abandoned start, not a lost recording.
      await patchEntry(entry.id, { state: "abandoned", lastError: "Recording never captured audio." });
      continue;
    }
    const bytesTotal = chunks.reduce((n, c) => n + c.byteLength, 0);
    const updated = await patchEntry(entry.id, {
      state: "pending",
      bytesTotal,
      lastError: "Recording was interrupted — uploading the audio captured before it stopped.",
    });
    if (updated) recovered.push(updated);
  }
  return recovered;
}

export type StorageHealth = {
  usageBytes: number;
  quotaBytes: number;
  /** Estimated minutes of recording the remaining space allows. */
  minutesRemaining: number;
  level: "ok" | "low" | "critical";
};

/**
 * The device-full case (checklist 2.4).
 *
 * Checked *before* recording rather than discovered mid-consultation: an
 * IndexedDB write that fails with QuotaExceededError halfway through a
 * consultation loses the rest of it, and there is no graceful recovery from
 * that in the moment. The queue is also the reason space frees up — audio is
 * deleted once the pipeline confirms — so a full disk usually means uploads
 * are stuck, which is itself the thing to surface.
 */
export async function checkStorage(bytesPerMinute = 240 * 1024): Promise<StorageHealth> {
  let usageBytes = 0;
  let quotaBytes = 0;
  try {
    const estimate = await navigator.storage?.estimate?.();
    usageBytes = estimate?.usage ?? 0;
    quotaBytes = estimate?.quota ?? 0;
  } catch {
    // Some browsers refuse the estimate in private mode. Reported as
    // unknown rather than assumed healthy.
  }

  const freeBytes = Math.max(0, quotaBytes - usageBytes);
  const minutesRemaining = quotaBytes > 0 ? Math.floor(freeBytes / bytesPerMinute) : Infinity;

  // Thresholds in minutes-of-recording, not percentages: "8% free" means
  // nothing to a doctor, "about 20 minutes of recording left" is actionable
  // and directly comparable to the length of a consultation.
  const level: StorageHealth["level"] =
    minutesRemaining === Infinity ? "ok" : minutesRemaining < 15 ? "critical" : minutesRemaining < 60 ? "low" : "ok";

  return { usageBytes, quotaBytes, minutesRemaining, level };
}

export function isQuotaError(error: unknown): boolean {
  return (
    error instanceof DOMException &&
    (error.name === "QuotaExceededError" || error.name === "NS_ERROR_DOM_QUOTA_REACHED")
  );
}

/* ------------------------------------------------------------------ *
 * The runner
 * ------------------------------------------------------------------ */

export type QueueTickResult = {
  processed: number;
  uploaded: number;
  confirmed: number;
  deleted: number;
  failed: number;
  offline: boolean;
};

let ticking = false;

/**
 * One pass over the queue. Safe to call often; re-entrant calls are skipped
 * rather than queued, because two concurrent passes would upload the same
 * parts twice and race on state transitions.
 */
export async function tick(): Promise<QueueTickResult> {
  const result: QueueTickResult = {
    processed: 0,
    uploaded: 0,
    confirmed: 0,
    deleted: 0,
    failed: 0,
    offline: false,
  };
  if (ticking) return result;
  ticking = true;

  try {
    const all = await listEntries();
    await recoverInterrupted(all);
    const entries = await listEntries();
    const now = Date.now();

    for (const entry of entries) {
      // --- stage 1: upload the bytes ---
      if ((entry.state === "pending" || entry.state === "uploading") && entry.nextAttemptAt <= now) {
        result.processed++;
        await patchEntry(entry.id, { state: "uploading" });
        try {
          // Wired to the progress bar (QueueStatus.tsx): without this,
          // bytesUploaded only ever changed once, from 0 to the final total,
          // the instant the whole upload finished — so a multi-part upload
          // sat showing "0 B of X" for its entire duration, indistinguishable
          // on screen from being stuck. One patch per completed part is the
          // real granularity uploadSession has to offer (Drive's protocol has
          // no finer-grained progress signal than "this whole part landed"),
          // but for anything longer than one part that is real, live movement
          // instead of a frozen number.
          const { objectKey, bytesUploaded } = await uploadSession(
            entry.id,
            entry.encounterId,
            (p) => void patchEntry(entry.id, { bytesUploaded: p.bytesUploaded }),
          );
          await patchEntry(entry.id, {
            state: "uploaded",
            objectKey,
            bytesUploaded,
            attempts: 0,
            lastError: null,
            nextAttemptAt: 0,
          });
          result.uploaded++;
        } catch (error) {
          if (error instanceof OfflineError) {
            result.offline = true;
            // Not a failure: no attempt counter increment, no backoff
            // escalation. Being offline is the expected state this whole
            // queue exists for, and counting it toward MAX_ATTEMPTS would
            // dead-letter a perfectly good recording during a wifi outage.
            await patchEntry(entry.id, { state: "pending", nextAttemptAt: now + 10_000 });
            continue;
          }

          const permanent = error instanceof PermanentUploadError;
          const attempts = entry.attempts + 1;
          const exhausted = permanent || attempts >= MAX_ATTEMPTS;
          await patchEntry(entry.id, {
            state: exhausted ? "failed" : "pending",
            attempts,
            lastError: (error as Error).message,
            nextAttemptAt: exhausted ? 0 : now + backoffMs(attempts),
          });
          if (exhausted) result.failed++;
          continue;
        }
      }

      // --- stage 2: wait for the pipeline, not the bytes ---
      const current = await getEntry(entry.id);
      // Also re-checked for a "failed" entry that already has an
      // objectKey: that failure happened *after* a successful upload, at
      // the server's pipeline stage — and the doctor's only way to fix
      // it is the pipeline retry on Home's Needs attention list, a
      // server-side action this queue's own tick() never otherwise
      // learns about. Without this, an entry that failed once stayed
      // stuck showing a stale error forever, even after the doctor fixed
      // it through that other path. Found live: the encounter had
      // already reached note_generated server-side while this exact
      // card still said "needs attention" and offered nothing but a
      // pointer elsewhere.
      const pipelineFailureEntry = current?.state === "failed" && current.objectKey !== null;
      if (current?.state === "uploaded" || pipelineFailureEntry) {
        try {
          const { started, terminalFailure } = await pipelineHasStarted(current.encounterId);
          if (started) {
            await patchEntry(current.id, { state: "confirmed", lastError: null });
            result.confirmed++;
          } else if (terminalFailure) {
            // The server's pipeline dead-lettered, or consent was
            // withdrawn. Local audio is KEPT: it may be the only copy, and
            // Phase 1.5's /retry can still make use of it.
            await patchEntry(current.id, {
              state: "failed",
              lastError: "The server could not process this recording. The audio is still on this laptop.",
            });
            result.failed++;
          }
        } catch {
          result.offline = true;
        }
      }

      // --- stage 3: only now delete local audio ---
      const afterConfirm = await getEntry(entry.id);
      if (afterConfirm?.state === "confirmed") {
        const removed = await deleteSession(afterConfirm.id);
        await patchEntry(afterConfirm.id, { state: "done" });
        result.deleted += removed;
      }
    }
  } finally {
    ticking = false;
  }

  return result;
}

/** Retries a failed entry at the doctor's request, clearing its backoff. */
export async function retryEntry(id: string): Promise<void> {
  await patchEntry(id, { state: "pending", attempts: 0, nextAttemptAt: 0, lastError: null });
  await tick();
}

let loopTimer: ReturnType<typeof setInterval> | undefined;

/**
 * Starts the background loop. Also ticks on `online`, because a doctor
 * walking back into wifi coverage should not wait out the interval — and
 * `visibilitychange`, since a backgrounded tab's timers are throttled and
 * returning to the app is the moment progress matters most.
 */
export function startQueueLoop(intervalMs = 15_000): () => void {
  const run = () => void tick();
  if (loopTimer) clearInterval(loopTimer);
  loopTimer = setInterval(run, intervalMs);
  window.addEventListener("online", run);
  document.addEventListener("visibilitychange", run);
  run();

  return () => {
    if (loopTimer) clearInterval(loopTimer);
    loopTimer = undefined;
    window.removeEventListener("online", run);
    document.removeEventListener("visibilitychange", run);
  };
}

/** For the status UI: which entries still hold audio a doctor could lose. */
export function summarise(entries: QueueEntry[]): {
  waiting: number;
  uploading: number;
  failed: number;
  holdingAudio: number;
  bytesPending: number;
} {
  const holds: QueueState[] = ["recording", "pending", "uploading", "uploaded", "confirmed", "failed"];
  return {
    waiting: entries.filter((e) => e.state === "pending").length,
    uploading: entries.filter((e) => e.state === "uploading").length,
    failed: entries.filter((e) => e.state === "failed").length,
    holdingAudio: entries.filter((e) => holds.includes(e.state)).length,
    bytesPending: entries
      .filter((e) => e.state === "pending" || e.state === "uploading")
      .reduce((n, e) => n + Math.max(0, e.bytesTotal - e.bytesUploaded), 0),
  };
}
