/**
 * The recording screen (checklist 2.2 / 2.3).
 *
 * Consent used to be captured in-app before this screen would let the
 * microphone open at all (the P0-1 gate, and the bilingual consent script
 * that went with it). That is now handled outside the app, so the record
 * button is available as soon as the screen loads — nothing here blocks on
 * a ledger check anymore.
 *
 * What phase 2.2 still owns:
 *   - the write-ahead queue entry, written before any audio exists, so a
 *     crash mid-recording is recovered rather than orphaned;
 *   - the device-full check, run at the moment of the tap rather than
 *     discovered mid-consultation;
 *   - the sticky recording indicator, so a patient in the room can tell at a
 *     glance that capture is live.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useRecorder } from "../lib/recorder/useRecorder";
import { RecordingIndicator } from "../components/RecordingIndicator";
import { Banner } from "../components/Banner";
import { formatBytes, formatDuration } from "../lib/format";
import { TARGET_BITS_PER_SECOND } from "../lib/audio-config";
import { useOnlineStatus } from "../lib/offline";
import {
  checkStorage,
  enqueueRecording,
  markReadyToUpload,
  markRecordingAlive,
} from "../lib/queue/queue";
import { useQueue } from "../lib/queue/useQueue";
import { QueueStatus, StorageWarning } from "../components/QueueStatus";

export function Record() {
  const { encounterId = "" } = useParams();
  const navigate = useNavigate();
  const online = useOnlineStatus();
  const { state, start, stop, isRecording } = useRecorder();
  const [summary, setSummary] = useState<string | null>(null);
  const [storageBlock, setStorageBlock] = useState<string | null>(null);
  const { entries, storage, retry, uploadNow } = useQueue();

  // Heartbeat while capturing, so the queue can distinguish a live recording
  // from one the app died in the middle of. Without it the queue "recovers"
  // an in-progress recording, tells the doctor it was interrupted, and starts
  // uploading before the last chunks exist.
  useEffect(() => {
    if (state.status !== "recording" && state.status !== "paused") return;
    const beat = () => void markRecordingAlive(encounterId);
    beat();
    const timer = setInterval(beat, 5000);
    return () => clearInterval(timer);
  }, [state.status, encounterId]);

  // Leaving mid-recording loses the un-flushed tail. The browser only allows
  // a generic prompt, but a generic prompt beats silent loss.
  useEffect(() => {
    if (!isRecording) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [isRecording]);

  const onStart = useCallback(async () => {
    // Device-full check happens here, not mid-consultation: an IndexedDB
    // write that fails with QuotaExceededError halfway through loses the rest
    // of the recording, and there is no graceful recovery in the moment.
    const health = await checkStorage();
    if (health.level === "critical") {
      setStorageBlock(
        `This laptop has only about ${health.minutesRemaining} minutes of recording space left. ` +
          "Let the upload queue finish, or free space, before starting a consultation.",
      );
      return;
    }
    setStorageBlock(null);

    // The write-ahead invariant: the record of intent — including the
    // idempotency key — reaches durable storage BEFORE any audio exists. A
    // crash mid-recording then leaves both the chunks and the intent on
    // disk, so the queue recovers the recording on next launch instead of
    // orphaning it.
    await enqueueRecording(encounterId, `enc-${encounterId}`);

    setSummary(null);
    try {
      await start(encounterId);
    } catch {
      // The session already surfaced the reason in state.error.
    }
  }, [encounterId, start]);

  const onStop = useCallback(async () => {
    const result = await stop();
    if (!result) return;

    // Hand it to the queue. The gap total travels with the entry so the
    // upload can eventually tell the server the audio is incomplete (2.2's
    // open follow-up), rather than that fact living only in this screen.
    // Only the gap total is passed: the byte total is read from the chunk
    // store, because `state.bytes` here predates stop()'s final flush.
    await markReadyToUpload(encounterId, { audioGapMs: result.missingMs });
    void uploadNow();

    const missing = result.missingMs >= 1000 ? formatDuration(result.missingMs) : null;
    setSummary(
      missing
        ? `Saved ${result.chunkCount} pieces to this laptop and queued for upload. ${missing} of audio is missing — see below.`
        : `Saved ${result.chunkCount} pieces to this laptop and queued for upload, with no audio gaps detected.`,
    );
  }, [stop, encounterId, uploadNow]);

  return (
    <main className="app">
      <RecordingIndicator
        active={state.status === "recording" || state.status === "paused"}
        paused={state.status === "paused"}
        elapsedMs={state.elapsedMs}
        missingMs={state.missingMs}
      />

      <header>
        <h1>Record consultation</h1>
        <div className="header-actions">
          <code>{encounterId.slice(0, 8)}</code>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              // useRecorder's own unmount cleanup stops an active recording
              // safely (flushes the last chunk, same as a real Stop tap) —
              // but doing that from a plain link click, with no warning,
              // is the click-away browser refresh already warns about via
              // beforeunload. Ask first, only when there's actually
              // something to lose.
              if (
                isRecording &&
                !window.confirm(
                  "Recording is still in progress. Leaving this page stops it now — the audio captured so far is saved and queued for upload. Continue?",
                )
              ) {
                return;
              }
              navigate("/");
            }}
          >
            Back to worklist
          </button>
        </div>
      </header>

      {!online && (
        <Banner tone="warn">
          No connection. Recording still works — audio is saved on this laptop and uploads later.
        </Banner>
      )}

      {state.error && <Banner tone="error">{state.error}</Banner>}
      {storageBlock && <Banner tone="error">{storageBlock}</Banner>}
      <StorageWarning storage={storage} />

      {/* --- controls --- */}
      <section className="card">
        <h2>Capture</h2>
        <p className="muted">
          Mono Opus at {TARGET_BITS_PER_SECOND / 1000} kbps, encrypted on this laptop before it
          touches disk, written in 5-second pieces so a crash costs at most one piece.
        </p>
        {state.status === "recording" || state.status === "paused" ? (
          <button type="button" onClick={() => void onStop()}>
            Stop recording
          </button>
        ) : (
          <button type="button" onClick={() => void onStart()} disabled={state.status === "starting"}>
            {state.status === "starting" ? "Starting…" : "Start recording"}
          </button>
        )}

        {summary && <Banner tone="info">{summary}</Banner>}
      </section>

      {/* --- live detail, shown while recording and after --- */}
      {state.status !== "idle" && (
        <section className="card">
          <h2>This recording</h2>
          <dl className="kv">
            <dt>Elapsed</dt>
            <dd>{formatDuration(state.elapsedMs)}</dd>
            <dt>Audio captured</dt>
            <dd>{formatDuration(state.capturedMs)}</dd>
            <dt>Missing</dt>
            <dd className={state.missingMs >= 1000 ? "bad" : undefined}>
              {formatDuration(state.missingMs)}
            </dd>
            {state.pausedMs > 0 && (
              <>
                <dt>Paused</dt>
                <dd>{formatDuration(state.pausedMs)}</dd>
              </>
            )}
            <dt>Saved on this laptop</dt>
            <dd>
              {state.chunkCount} pieces · {formatBytes(state.bytes)}
            </dd>
            {state.deviceLabel && (
              <>
                <dt>Microphone</dt>
                <dd>{state.deviceLabel}</dd>
              </>
            )}
            {state.mimeType && (
              <>
                <dt>Format</dt>
                <dd>{state.mimeType}</dd>
              </>
            )}
          </dl>

          {state.gaps.length > 0 && (
            <Banner tone="error">
              <span>
                <strong>
                  {state.gaps.length} gap{state.gaps.length === 1 ? "" : "s"} in the audio.
                </strong>{" "}
                {state.gaps.some((g) => g.cause === "suspend")
                  ? "The laptop went to sleep during the recording — most likely the lid was closed. No software can capture audio while the machine is suspended, so that time is genuinely missing from the record."
                  : "The audio pipeline stalled. That time is missing from the record."}
              </span>
            </Banner>
          )}

          {state.mismatches.length > 0 && (
            <Banner tone="warn">
              <span>
                The microphone ignored{" "}
                {state.mismatches.map((m) => `${m.field} (asked ${m.requested}, got ${m.actual})`).join(", ")}
                . Recording continues — the bitrate is set explicitly, so this does not inflate the
                upload.
              </span>
            </Banner>
          )}
        </section>
      )}

      <QueueStatus entries={entries} storage={storage} onRetry={retry} onUploadNow={uploadNow} />
    </main>
  );
}
