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
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { QueueEntry } from "../lib/queue/store";
import type { StorageHealth } from "../lib/queue/queue";
import { Banner } from "./Banner";
import { useToast } from "./Toast";
import { formatBytes, formatDuration } from "../lib/format";

/**
 * What to actually tell the doctor, per state — not a fixed label, because
 * "Waiting to upload" meant the same thing whether an upload was about to
 * start in the next tick or was backed off ten minutes behind a wifi outage,
 * and "Uploaded — waiting for the server" gave no sense of whether that was
 * two seconds or two minutes. Found live: audio that had already reached
 * Google Drive was still shown this way, indistinguishable from actually
 * being stuck. Every branch here is honest about what is and isn't known —
 * a real percentage where one exists, an elapsed clock and a "taking longer
 * than usual" flag where it doesn't, rather than papering over the gap with
 * a friendlier-sounding static sentence.
 */
function describeEntry(
  entry: QueueEntry,
  now: number,
): { label: string; detail: string | null; percent: number | null; indeterminate: boolean } {
  switch (entry.state) {
    case "recording":
      return { label: "Recording now", detail: null, percent: null, indeterminate: true };

    case "pending": {
      const waitMs = entry.nextAttemptAt - now;
      if (waitMs > 1000) {
        // nextAttemptAt in the future means this isn't idle -- it already
        // tried and is backed off (offline, or a transient failure), not
        // sitting untouched. Naming that is the difference between "nothing
        // is happening" and "something happened, here's when it tries again".
        return {
          label: "Waiting to retry",
          detail:
            entry.attempts > 0
              ? `Attempt ${entry.attempts} didn't go through — retrying in ${Math.ceil(waitMs / 1000)}s`
              : `Retrying in ${Math.ceil(waitMs / 1000)}s`,
          percent: null,
          indeterminate: true,
        };
      }
      return { label: "Queued to upload", detail: "Starting shortly", percent: null, indeterminate: true };
    }

    case "uploading": {
      const percent =
        entry.bytesTotal > 0 ? Math.min(100, Math.round((entry.bytesUploaded / entry.bytesTotal) * 100)) : null;
      return {
        label: "Uploading",
        detail: percent !== null ? `${formatBytes(entry.bytesUploaded)} of ${formatBytes(entry.bytesTotal)}` : null,
        percent,
        // No percent yet (bytesTotal not known this tick) is the only case
        // with truly nothing to show — everything else gets the real number.
        indeterminate: percent === null,
      };
    }

    case "uploaded": {
      const elapsedS = Math.max(0, Math.round((now - entry.updatedAt) / 1000));
      // The bytes are already on Drive/S3 at this point (upload/complete
      // returned 200) -- what's left is entirely server-side transcription
      // and note generation, which this laptop cannot see the progress of.
      // An honest "still going" beats a silent number that never moves.
      return {
        label: "Uploaded — the server is starting to process it",
        detail:
          elapsedS > 90
            ? `Still waiting after ${formatDuration(elapsedS * 1000)} — this is taking longer than usual`
            : `Reached the server ${elapsedS}s ago`,
        percent: null,
        indeterminate: true,
      };
    }

    case "confirmed":
      return {
        label: "Server is processing this recording",
        detail: "Clearing the local copy now that the server has it",
        percent: null,
        indeterminate: true,
      };

    case "failed":
      return { label: "Needs attention", detail: null, percent: null, indeterminate: false };

    case "abandoned":
    case "done":
      return { label: entry.state === "done" ? "Done" : "Consent withdrawn", detail: null, percent: null, indeterminate: false };
  }
}

/** A small circular indicator: a filling ring for a known percentage, a
 * spinning one when only "still working" is known. Never shown at all for
 * a state with nothing left to wait on. */
function QueueRing({ percent, indeterminate }: { percent: number | null; indeterminate: boolean }) {
  if (percent === null && !indeterminate) return null;
  const style = { "--queue-ring-pct": `${percent ?? 0}%` } as React.CSSProperties;
  return (
    <span
      className={`queue-ring${indeterminate ? " is-indeterminate" : ""}`}
      style={style}
      role="img"
      aria-label={percent !== null ? `${percent}% uploaded` : "Working"}
    />
  );
}

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

  // Re-renders once a second purely so the "retrying in Xs" countdown and
  // the "reached the server Ns ago" clock actually move — useQueue's own
  // 3s poll would otherwise leave both looking frozen between polls, which
  // is exactly the "is this stuck?" impression this component exists to
  // rule out. Only runs while there is something to count for; an idle
  // queue costs nothing.
  const [, retick] = useState(0);
  const hasTimedEntry = active.some((e) => e.state !== "failed");
  useEffect(() => {
    if (!hasTimedEntry) return;
    const id = setInterval(() => retick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [hasTimedEntry]);
  const now = Date.now();

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
              const described = describeEntry(entry, now);
              return (
                <li key={entry.id} className={entry.state === "failed" ? "queue-item is-failed" : "queue-item"}>
                  <div className="queue-head">
                    <code>{entry.encounterId.slice(0, 8)}</code>
                    <QueueRing percent={described.percent} indeterminate={described.indeterminate} />
                    <span className="queue-state">{described.label}</span>
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
                  {described.detail && <p className="queue-detail">{described.detail}</p>}
                  <div className="queue-meta">
                    {/* Only shown where `described.detail` doesn't already say it --
                        uploading/uploaded both cover the byte count there already,
                        this would just repeat it. */}
                    {entry.bytesTotal > 0 && (entry.state === "recording" || entry.state === "pending") && (
                      <span>{formatBytes(entry.bytesTotal)} recorded</span>
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
