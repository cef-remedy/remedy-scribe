/**
 * Turns a finished recording into an uploaded, pipeline-confirmed encounter.
 *
 * The two granularities that must not be conflated (decision 0026 §3):
 *
 *   - **recorder chunks** — ~5s / ~20 KB, small so a crash costs one chunk
 *   - **S3 parts** — ≥5 MB except the last, so at mono Opus 32 kbps a part
 *     is ~21 minutes of audio
 *
 * So this assembles many chunks into each part. A typical consult is one
 * part (the last, which is exempt from the minimum); a very long one is two.
 * Getting this backwards — one part per chunk — would be rejected by S3 for
 * every part but the final one.
 *
 * Resumability comes from S3 itself: `GET /upload/parts` reports what has
 * already landed (decision 0013 chose that over mirroring part state in
 * Postgres), so a resumed upload skips completed parts rather than
 * restarting.
 */
import { api, OfflineError } from "../../api/client";
import { decryptChunk, getAudioKey } from "../recorder/crypto";
import { readSessionChunks } from "../recorder/store";

/** S3's floor for every part except the last. Mirrors MIN_PART_SIZE_BYTES. */
export const MIN_PART_SIZE_BYTES = 5 * 1024 * 1024;

export type UploadProgress = {
  partNumber: number;
  bytesUploaded: number;
  bytesTotal: number;
};

export class PermanentUploadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PermanentUploadError";
  }
}

/**
 * Cuts a recording into parts on **exact byte boundaries**.
 *
 * Every part but the last is exactly `partSize`. That is not tidiness — it
 * is the contract `storage_drive.py` documents and depends on ("every part
 * but the last is exactly `MIN_PART_SIZE_BYTES`, which the client
 * guarantees"): Drive persists a resumable upload in 256 KiB increments and
 * reports progress as a *byte offset*, which its adapter floor-divides back
 * into part numbers. A part that is not an exact multiple desynchronises the
 * two, permanently.
 *
 * This used to group whole recorder chunks until their running total first
 * *reached* the minimum. S3 only requires "at least", so that was correct
 * there and silently wrong on Drive: real ~5s Opus chunks are ~17 KB and
 * never sum to exactly 256 KiB, so part 1 overshot the boundary by part of a
 * chunk, Drive kept only the aligned prefix and answered 308, and part 2 then
 * announced a `Content-Range` starting at a byte Drive had never reached.
 * Found live: a 1:35 consultation stuck on "Part 2 upload failed (HTTP 503)"
 * through all 8 attempts — deterministic, not flaky, because every retry
 * re-derived the same wrong offset from the same ragged plan while
 * `/upload/parts` kept correctly reporting the aligned 262,144 bytes Drive
 * actually held.
 *
 * Slicing bytes rather than grouping chunks is what lets a part end mid-chunk,
 * which is unavoidable once the boundary is fixed.
 *
 * Pure and exported so the arithmetic can be tested without a network — it is
 * exactly the sort that looks obviously right and is off by one part.
 */
export function planParts(
  totalBytes: number,
  partSize = MIN_PART_SIZE_BYTES,
): { start: number; end: number; bytes: number }[] {
  if (totalBytes <= 0) return [];

  const parts: { start: number; end: number; bytes: number }[] = [];
  let start = 0;
  // `>` rather than `>=`: a remainder of exactly `partSize` is the final
  // part, not a full part trailed by an empty one that storage would reject.
  while (totalBytes - start > partSize) {
    parts.push({ start, end: start + partSize, bytes: partSize });
    start += partSize;
  }
  // The last part carries the remainder. Both backends exempt only this one
  // from the size rule — S3's 5 MiB floor and Drive's 256 KiB multiple alike.
  parts.push({ start, end: totalBytes, bytes: totalBytes - start });
  return parts;
}

/**
 * Uploads one recording. Returns once `upload/complete` has succeeded —
 * which confirms the *bytes*, not the pipeline. The queue separately waits
 * for `pipeline_status` before deleting local audio (P0-2).
 *
 * Throws `PermanentUploadError` for anything a retry cannot fix, `OfflineError`
 * for a network failure, and a plain Error for a transient server problem.
 */
export async function uploadSession(
  sessionId: string,
  encounterId: string,
  onProgress?: (p: UploadProgress) => void,
): Promise<{ objectKey: string; bytesUploaded: number }> {
  const chunks = await readSessionChunks(sessionId);
  if (chunks.length === 0) {
    // Nothing on disk. Permanent: retrying cannot conjure audio, and the
    // likely cause is a withdrawal having already shredded it.
    throw new PermanentUploadError("No local audio remains for this recording.");
  }
  // Every chunk in a session shares one mime type, set once by the recorder
  // and stored unencrypted on the chunk record itself (recorder/store.ts) —
  // so it is known before a single byte is decrypted. That is what lets the
  // next line start `upload/init`'s network round trip immediately, running
  // alongside decryption instead of waiting behind it.
  const mimeType = chunks[0].mimeType;

  // --- init (idempotent: returns the existing session on retry) ---
  const initPromise = api.POST("/api/v1/encounters/{encounter_id}/upload/init", {
    params: { path: { encounter_id: encounterId } },
    body: { content_type: mimeType },
  });

  // Decryption used to be one `await` per chunk in a for-loop, serialising
  // WebCrypto calls that share no state and have nothing to serialise for —
  // Promise.all runs them concurrently instead (order preserved, since
  // Promise.all's results array mirrors its input array). Found live: a
  // longer consult's few hundred ~5s chunks measurably slowed the "getting
  // ready to upload" phase before a single upload byte had left the laptop,
  // which is exactly the phase the doctor sees no progress indicator for.
  const key = await getAudioKey();
  const decryptPromise = Promise.all(
    chunks.map((chunk) => decryptChunk(key, { ciphertext: chunk.ciphertext, iv: chunk.iv })),
  );

  const [init, plaintextChunks] = await Promise.all([initPromise, decryptPromise]);
  const bytesTotal = plaintextChunks.reduce((n, b) => n + b.byteLength, 0);

  if (init.error || !init.data) {
    if (init.response.status === 409) {
      throw new PermanentUploadError(
        "The server says this recording was already uploaded and finalised.",
      );
    }
    throw new Error(`Could not start the upload (HTTP ${init.response.status}).`);
  }
  const objectKey = init.data.object_key;

  // The server reports the part size its backend requires — S3 wants 5 MiB
  // parts, Google Drive wants multiples of 256 KiB (decision 0040). Reading
  // it rather than assuming S3's floor is what lets the backend change
  // without touching this file.
  const plan = planParts(bytesTotal, init.data.min_part_size_bytes || MIN_PART_SIZE_BYTES);

  // One Blob over the whole recording, sliced on the plan's exact boundaries.
  // `Blob.slice` is a view, not a copy, so this costs nothing beyond the
  // chunks already in memory — and it is what allows a part to end mid-chunk,
  // which fixed-size parts require and chunk-grouping could never do.
  const audio = new Blob(plaintextChunks, { type: mimeType });

  // --- what already landed? S3 is the source of truth (decision 0013) ---
  const existing = await api.GET("/api/v1/encounters/{encounter_id}/upload/parts", {
    params: { path: { encounter_id: encounterId } },
  });
  const done = new Set<number>(
    (existing.data?.parts ?? []).map((p: { part_number: number }) => p.part_number),
  );

  let bytesUploaded = 0;

  for (let index = 0; index < plan.length; index++) {
    const partNumber = index + 1; // S3 parts are 1-based
    const partBytes = plan[index].bytes;

    if (done.has(partNumber)) {
      // Already accepted by S3 on an earlier attempt. Skipping is the whole
      // point of resumability — re-sending would be correct but wasteful,
      // and on clinic wifi that waste is the difference that matters.
      bytesUploaded += partBytes;
      onProgress?.({ partNumber, bytesUploaded, bytesTotal });
      continue;
    }

    const presigned = await api.POST("/api/v1/encounters/{encounter_id}/upload/parts/{part_number}", {
      params: { path: { encounter_id: encounterId, part_number: partNumber } },
    });
    if (presigned.error || !presigned.data) {
      throw new Error(`Could not get an upload URL for part ${partNumber}.`);
    }

    const body = audio.slice(plan[index].start, plan[index].end, mimeType);

    // Straight to S3, not through our API — the presigned URL exists so the
    // audio never routes through the application server (decision 0013).
    // Deliberately a bare fetch: this must NOT carry our Authorization
    // header or cookies, since S3 rejects requests with an unexpected auth
    // header alongside a presigned signature.
    // Byte offsets for this part, which a resumable backend needs and S3
    // ignores. Read straight off the plan — the same numbers the body was
    // sliced with, so the header and the bytes cannot disagree.
    const partStart = plan[index].start;
    const partEnd = plan[index].end - 1;

    let response: Response;
    try {
      response = await fetch(presigned.data.url, {
        method: "PUT",
        // `Content-Range` is required by Google Drive's resumable protocol
        // (every chunk says where it sits in the whole file) and is ignored
        // by S3, which addresses each part by its own presigned URL. Sending
        // it unconditionally keeps one code path for both backends.
        headers: { "Content-Range": `bytes ${partStart}-${partEnd}/${bytesTotal}` },
        body,
      });
    } catch {
      throw new OfflineError();
    }

    // 308 "Resume Incomplete" is Drive's *success* for every chunk but the
    // last: it means "stored, send the next one". `response.ok` is false for
    // it, so treating ok-ness as success would fail every multi-part upload
    // on Drive at the first chunk.
    const accepted = response.ok || response.status === 308;
    if (!accepted) {
      if (response.status === 403) {
        // Almost always an expired presigned URL. Transient: the next
        // attempt mints a fresh one.
        throw new Error(`Upload URL for part ${partNumber} was rejected (likely expired).`);
      }
      throw new Error(`Part ${partNumber} upload failed (HTTP ${response.status}).`);
    }

    bytesUploaded += partBytes;
    onProgress?.({ partNumber, bytesUploaded, bytesTotal });
  }

  // --- complete: confirms the bytes, and kicks the pipeline ---
  const complete = await api.POST("/api/v1/encounters/{encounter_id}/upload/complete", {
    params: { path: { encounter_id: encounterId } },
  });
  if (complete.error || !complete.data) {
    if (complete.response.status === 409) {
      // Consent withdrawn between capture and upload, or already completed.
      // Either way a retry cannot help.
      throw new PermanentUploadError(
        "The server refused to finalise this upload — consent may have been withdrawn.",
      );
    }
    throw new Error(`Could not finalise the upload (HTTP ${complete.response.status}).`);
  }

  return { objectKey, bytesUploaded };
}

/**
 * Has the server's pipeline actually started on this encounter?
 *
 * P0-2 requires local audio to survive until receipt *and* pipeline start,
 * and the checklist is sharper: "the confirmation the device waits for
 * should be about the pipeline, not the bytes." `uploaded` means only that
 * bytes arrived and work was enqueued — a worker that never runs would
 * leave that status forever while the laptop deleted its only copy.
 *
 * `transcribed` is the first status that proves work happened, and it is
 * also the point at which the transcript exists server-side, so the audio is
 * no longer the sole record of what was said.
 */
export async function pipelineHasStarted(
  encounterId: string,
): Promise<{ started: boolean; status: string | null; terminalFailure: boolean }> {
  const { data, error, response } = await api.GET("/api/v1/encounters/{encounter_id}", {
    params: { path: { encounter_id: encounterId } },
  });
  if (error || !data) {
    if (response.status === 404) {
      return { started: false, status: null, terminalFailure: true };
    }
    return { started: false, status: null, terminalFailure: false };
  }

  const status = data.pipeline_status;
  const started = status === "transcribed" || status === "note_generated";
  // A dead-lettered or consent-blocked encounter will never advance on its
  // own. Local audio must be KEPT in that case — it may be the only copy —
  // so this is reported rather than treated as confirmation.
  const terminalFailure =
    status === "transcription_failed" ||
    status === "generation_failed" ||
    status === "blocked_no_consent";

  return { started, status, terminalFailure };
}
