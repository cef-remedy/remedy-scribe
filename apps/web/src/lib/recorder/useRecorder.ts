/**
 * React binding for RecordingSession. Thin on purpose: the session owns all
 * the timing, gap detection, and crypto, and this only mirrors its state
 * into render. Keeping the audio machinery out of React means a re-render
 * can never restart a recording.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { RecordingSession, type RecordingState } from "./session";

export function useRecorder() {
  const sessionRef = useRef<RecordingSession | null>(null);
  const [state, setState] = useState<RecordingState>({
    status: "idle",
    elapsedMs: 0,
    capturedMs: 0,
    missingMs: 0,
    chunkCount: 0,
    bytes: 0,
    pausedMs: 0,
    gaps: [],
    mismatches: [],
    mimeType: null,
    sampleRate: null,
    deviceLabel: null,
    error: null,
  });

  const start = useCallback(async (sessionId: string) => {
    const session = new RecordingSession(sessionId);
    sessionRef.current = session;
    session.subscribe(setState);
    await session.start();
  }, []);

  const stop = useCallback(async () => {
    return sessionRef.current?.stop() ?? null;
  }, []);

  /** Mid-visit re-consent (P0-1): pause, log fresh consent, resume. */
  const pause = useCallback(async () => {
    await sessionRef.current?.pause();
  }, []);

  const resume = useCallback(async () => {
    await sessionRef.current?.resume();
  }, []);

  useEffect(() => {
    return () => {
      // Unmounting while recording must not leave a live microphone and an
      // orphaned wake lock behind. Fire-and-forget is fine here: stop()
      // awaits its own pending chunk writes.
      const session = sessionRef.current;
      const status = session?.getState().status;
      if (session && (status === "recording" || status === "paused")) void session.stop();
    };
  }, []);

  /**
   * A recording is in progress and leaving would lose the tail. Wired to
   * beforeunload by the recording screen rather than here, so the warning
   * is attached to the UI that can explain it.
   */
  const isRecording =
    state.status === "recording" || state.status === "starting" || state.status === "paused";

  return {
    state,
    start,
    stop,
    pause,
    resume,
    isRecording,
    sessionId: sessionRef.current?.sessionId ?? null,
  };
}
