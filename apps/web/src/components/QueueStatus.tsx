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
import type { QueueEntry } from "../lib/queue/store";
import type { StorageHealth } from "../lib/queue/queue";
import { Banner } from "./Banner";
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
            {active.map((entry) => (
              <li key={entry.id} className={entry.state === "failed" ? "queue-item is-failed" : "queue-item"}>
                <div className="queue-head">
                  <code>{entry.encounterId.slice(0, 8)}</code>
                  <span className="queue-state">{STATE_LABEL[entry.state]}</span>
                  {entry.state === "failed" && (
                    <button type="button" className="ghost" onClick={() => onRetry(entry.id)}>
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
              </li>
            ))}
          </ul>
          <button type="button" className="ghost" onClick={onUploadNow}>
            Try uploading now
          </button>
        </>
      )}
    </section>
  );
}
