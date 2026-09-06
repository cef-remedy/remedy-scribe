/**
 * The visible, persistent queue status (P0-2: "the doctor sees a visible,
 * persistent queue status for any recording not yet uploaded", and the PRD's
 * blanket requirement that nothing fails silently).
 *
 * Persistent means it does not disappear on its own. A recording that has
 * not reached the server is a consultation at risk of being lost, and the
 * doctor is the only person who can act on that — so it stays on screen
 * until it is genuinely resolved, and a failed entry stays until retried.
 */
import { Link } from "react-router-dom";
import type { QueueEntry } from "../lib/queue/store";
import type { StorageHealth } from "../lib/queue/queue";
import { Banner } from "./Banner";
import { useToast } from "./Toast";
import { formatBytes, formatDuration } from "../lib/format";

const STATE_LABEL: Record<QueueEntry["state"], string> = {
  recording: "Recording now",
  pending: "Waiting to upload",
  uploading: "Uploading",
  uploaded: "Uploaded — waiting for the server to start processing",
  confirmed: "Processing started — clearing local copy",
  done: "Done",
  failed: "Needs attention",
  abandoned: "Consent withdrawn",
};

export function StorageWarning({ storage }: { storage: StorageHealth | null }) {
  if (!storage || storage.level === "ok") return null;
  const minutes = storage.minutesRemaining;
  return (
    <Banner tone={storage.level === "critical" ? "error" : "warn"}>
      <span>
        <strong>
          {storage.level === "critical" ? "This laptop is nearly full." : "Storage is getting low."}
        </strong>{" "}
        Roughly {minutes} minute{minutes === 1 ? "" : "s"} of recording space left
        {" "}({formatBytes(storage.quotaBytes - storage.usageBytes)} free). Space frees up as
        recordings finish uploading — if it keeps shrinking, uploads are stuck and the queue below
        will say why.
      </span>
    </Banner>
  );
}

export function QueueStatus({
  entries,
  storage,
  onRetry,
  onUploadNow,
}: {
  entries: QueueEntry[];
  storage: StorageHealth | null;
  onRetry: (id: string) => void;
  onUploadNow: () => void;
}) {
  const { showToast } = useToast();
  // `done` and `abandoned` are resolved; showing them forever would bury the
  // entries that still need something.
  const active = entries.filter((e) => e.state !== "done" && e.state !== "abandoned");
  const failed = active.filter((e) => e.state === "failed");

  return (
    <section className="card">
      <h2>Upload queue</h2>
      <StorageWarning storage={storage} />

      {active.length === 0 ? (
        <p className="muted">
          Nothing waiting. Every recording has reached the server and been processed.
        </p>
      ) : (
        <>
          <p className="muted">
            {active.length} recording{active.length === 1 ? "" : "s"} still on this laptop.
            {failed.length > 0 && " Some need your attention."}
          </p>
          <ul className="queue">
            {active.map((entry) => {
              // objectKey is only ever set once upload/complete has
              // actually returned 200 (store.ts's own comment on the
              // field) and is never cleared afterward -- so a "failed"
              // entry that still has one didn't fail to upload, it
              // failed *after*, at the server's pipeline stage. Retrying
              // the upload for that case isn't a no-op, it's actively
              // wrong: the bytes are already there, the server correctly
              // 409s "already uploaded and finalised", and the doctor
              // sees a dead-end error instead of the real fix, which is
              // the pipeline retry on Home's Needs attention list. Found
              // live: exactly that click, on this exact message.
              const pipelineFailure = entry.state === "failed" && entry.objectKey !== null;
              return (
                <li key={entry.id} className={entry.state === "failed" ? "queue-item is-failed" : "queue-item"}>
                  <div className="queue-head">
                    <code>{entry.encounterId.slice(0, 8)}</code>
                    <span className="queue-state">{STATE_LABEL[entry.state]}</span>
                    {entry.state === "failed" && !pipelineFailure && (
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => {
                          onRetry(entry.id);
                          // Fire-and-forget: retry/upload here report success
                          // or failure through the queue state itself over
                          // the next poll, not through this click — so the
                          // toast only ever confirms the tap, in present
                          // tense, never a result. Nothing to miss by
                          // looking away; the entry stays on screen either
                          // way until it is genuinely resolved (this file's
                          // own header comment on why it's persistent).
                          showToast("Retry queued.");
                        }}
                      >
                        Retry
                      </button>
                    )}
                  </div>
                  <div className="queue-meta">
                    {entry.bytesTotal > 0 && (
                      <span>
                        {formatBytes(entry.bytesUploaded)} of {formatBytes(entry.bytesTotal)}
                      </span>
                    )}
                    {entry.attempts > 0 && (
                      <span>
                        {entry.attempts} failed attempt{entry.attempts === 1 ? "" : "s"}
                      </span>
                    )}
                    {entry.audioGapMs >= 1000 && (
                      <span className="bad">{formatDuration(entry.audioGapMs)} of audio missing</span>
                    )}
                  </div>
                  {entry.lastError && <p className="queue-error">{entry.lastError}</p>}
                  {pipelineFailure && (
                    <p className="queue-error">
                      The recording reached the server fine — retrying here would only ask it to
                      upload again, which it will refuse. Go to{" "}
                      <Link to="/">Home → Needs attention</Link> to retry processing instead.
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              onUploadNow();
              showToast("Upload started.");
            }}
          >
            Try uploading now
          </button>
        </>
      )}
    </section>
  );
}
