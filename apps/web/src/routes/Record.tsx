/**
 * The recording screen (checklist 2.2).
 *
 * The consent gate is the first thing that happens and the only path to the
 * record button. P0-1 requires the app to block recording "before anything
 * is captured", so the gate is checked before `getUserMedia` is ever called
 * — not after, and not in parallel.
 *
 * Phase 2.3 will replace the "capture consent" branch with the real
 * bilingual consent flow. Until it exists, a doctor genuinely cannot record,
 * which is the correct state for a system whose legal basis for recording is
 * not yet implemented — rather than a temporarily-open path with a TODO.
 */
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { checkConsentGate, type ConsentGate } from "../lib/consent";
import { useRecorder } from "../lib/recorder/useRecorder";
import { RecordingIndicator } from "../components/RecordingIndicator";
import { Banner } from "../components/Banner";
import { formatBytes, formatDuration } from "../lib/format";
import { TARGET_BITS_PER_SECOND } from "../lib/audio-config";
import { useOnlineStatus } from "../lib/offline";

export function Record() {
  const { encounterId = "" } = useParams();
  const online = useOnlineStatus();
  const { state, start, stop, isRecording } = useRecorder();
  const [gate, setGate] = useState<ConsentGate | null>(null);
  const [summary, setSummary] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void checkConsentGate(encounterId).then((result) => {
      if (!cancelled) setGate(result);
    });
    return () => {
      cancelled = true;
    };
  }, [encounterId]);

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
    // Re-checked at the moment of the tap, not just on mount: consent can be
    // withdrawn while this screen sits open, and the mount-time answer would
    // be stale. Cheap, and it is the difference between a gate and a hint.
    const fresh = await checkConsentGate(encounterId);
    setGate(fresh);
    if (!fresh.allowed) return;

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
    const missing = result.missingMs >= 1000 ? formatDuration(result.missingMs) : null;
    setSummary(
      missing
        ? `Saved ${result.chunkCount} pieces to this laptop. ${missing} of audio is missing — see below.`
        : `Saved ${result.chunkCount} pieces to this laptop, with no audio gaps detected.`,
    );
  }, [stop]);

  return (
    <main className="app">
      <RecordingIndicator
        active={state.status === "recording"}
        elapsedMs={state.elapsedMs}
        missingMs={state.missingMs}
      />

      <header>
        <h1>Record consultation</h1>
        <code>{encounterId.slice(0, 8)}</code>
      </header>

      {!online && (
        <Banner tone="warn">
          No connection. Recording still works — audio is saved on this laptop and uploads later.
        </Banner>
      )}

      {state.error && <Banner tone="error">{state.error}</Banner>}

      {/* --- the consent gate (P0-1) --- */}
      {gate === null && <p className="muted">Checking consent for this encounter…</p>}

      {gate && !gate.allowed && (
        <Banner tone="error">
          <span>
            <strong>Recording is blocked.</strong> {gate.reason}
            {gate.needsConsentFlow && (
              <>
                {" "}
                The bilingual consent flow is Phase 2.3 and does not exist yet, so this cannot be
                resolved from the app today.
              </>
            )}
          </span>
        </Banner>
      )}

      {/* --- controls --- */}
      <section className="card">
        <h2>Capture</h2>
        {gate?.allowed ? (
          <>
            <p className="muted">
              Mono Opus at {TARGET_BITS_PER_SECOND / 1000} kbps, encrypted on this laptop before it
              touches disk, written in 5-second pieces so a crash costs at most one piece.
            </p>
            {state.status !== "recording" ? (
              <button type="button" onClick={() => void onStart()} disabled={state.status === "starting"}>
                {state.status === "starting" ? "Starting…" : "Start recording"}
              </button>
            ) : (
              <button type="button" onClick={() => void onStop()}>
                Stop recording
              </button>
            )}
          </>
        ) : (
          <p className="muted">
            The record control appears once consent has been captured for this encounter.
          </p>
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
    </main>
  );
}
