/**
 * Playing one cited passage of a recording (Phase 3, P0-7).
 *
 * Three things here are deliberate:
 *
 * 1. **It plays a window, not a file.** Playback stops at the cited
 *    passage's `end_ms` instead of running on into the rest of the
 *    consultation. A doctor asked to hear one line's source; continuing past
 *    it discloses more of the recording than they asked for, and does it
 *    without them noticing.
 *
 * 2. **The bytes are never stored.** The `<audio>` element streams from a
 *    presigned URL signed with `Cache-Control: no-store`, so the browser
 *    fetches only the seconds it plays (via its own Range requests, straight
 *    to object storage) and writes none of it to disk. This is P0-7's
 *    "without permanently re-downloading PHI", and it is why nothing here
 *    touches IndexedDB — unlike the recorder, which must persist locally.
 *
 * 3. **The URL is short-lived and re-fetched, not cached.** A presigned URL
 *    is a working handle on a recording. Holding one in memory across a long
 *    review session would keep that handle alive far past the click that
 *    justified it, so it is refetched when it expires.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchPlaybackUrl } from "./grounding";

/** Re-fetch slightly before the server's expiry rather than discovering it mid-seek. */
const EXPIRY_SAFETY_MARGIN_MS = 15_000;

/**
 * A short tail past the passage's own end.
 *
 * `end_ms` is the last word's end as the ASR reported it, and both that
 * timestamp and the `timeupdate` tick (~250ms) are coarser than speech. Cutting
 * at exactly `end_ms` clips the final word often enough to matter when the
 * whole point is letting a doctor hear what was actually said. A quarter second
 * fixes that and discloses nothing meaningful — unlike simply letting playback
 * run on, which would keep disclosing the consultation indefinitely.
 */
const PLAYBACK_TAIL_MS = 250;

export type PassagePlayer = {
  playing: boolean;
  /** The segment id currently sounding, if any — used to mark the passage in the UI. */
  playingSegmentId: string | null;
  /** 0–1 through the current passage window. For the chase-light playhead —
   *  real position, not a generic indeterminate spinner. 0 when nothing is
   *  playing. */
  progress: number;
  error: string | null;
  play: (segmentId: string, startMs: number, endMs: number) => Promise<void>;
  stop: () => void;
};

export function usePassagePlayer(encounterId: string | null): PassagePlayer {
  const audio = useMemo(() => {
    if (typeof Audio === "undefined") return null; // SSR / test environments
    const el = new Audio();
    // Nothing is fetched until a doctor actually asks for a passage.
    el.preload = "none";
    return el;
  }, []);

  const [playing, setPlaying] = useState(false);
  const [playingSegmentId, setPlayingSegmentId] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const url = useRef<{ value: string; expiresAt: number } | null>(null);
  const startAtMs = useRef<number | null>(null);
  const stopAtMs = useRef<number | null>(null);

  const stop = useCallback(() => {
    if (audio) audio.pause();
    startAtMs.current = null;
    stopAtMs.current = null;
    setPlaying(false);
    setPlayingSegmentId(null);
    setProgress(0);
  }, [audio]);

  useEffect(() => {
    if (!audio) return;

    const onTimeUpdate = () => {
      // The window boundary. `timeupdate` fires every ~250ms, which is close
      // enough for a passage and costs nothing; a precise stop would need an
      // AudioWorklet and buys nothing a doctor would notice. Progress rides
      // the same tick — one listener, not a second poller.
      if (startAtMs.current !== null && stopAtMs.current !== null) {
        const span = stopAtMs.current - startAtMs.current;
        const at = audio.currentTime * 1000 - startAtMs.current;
        setProgress(span > 0 ? Math.min(1, Math.max(0, at / span)) : 0);
      }
      if (stopAtMs.current !== null && audio.currentTime * 1000 >= stopAtMs.current) stop();
    };
    const onEnded = () => stop();
    const onError = () => {
      // Most likely an expired URL. Drop it so the next click mints a fresh
      // one rather than retrying a dead handle.
      url.current = null;
      setError("That passage could not be played. Try again.");
      stop();
    };

    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("error", onError);
    return () => {
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("ended", onEnded);
      audio.removeEventListener("error", onError);
      // Leaving the screen must not leave a stream open on a recording.
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    };
  }, [audio, stop]);

  const play = useCallback(
    async (segmentId: string, startMs: number, endMs: number) => {
      if (!audio || !encounterId) return;
      setError(null);

      if (!url.current || Date.now() >= url.current.expiresAt) {
        const result = await fetchPlaybackUrl(encounterId);
        if ("error" in result) {
          setError(result.error);
          return;
        }
        url.current = {
          value: result.url,
          expiresAt: Date.now() + result.expiresInSeconds * 1000 - EXPIRY_SAFETY_MARGIN_MS,
        };
        audio.src = result.url;
      }

      startAtMs.current = startMs;
      stopAtMs.current = endMs + PLAYBACK_TAIL_MS;
      try {
        audio.currentTime = startMs / 1000;
        await audio.play();
        setPlaying(true);
        setPlayingSegmentId(segmentId);
        setProgress(0);
      } catch {
        // Autoplay policy, or a seek before metadata loaded. Either way the
        // doctor gets a reason rather than a button that appears to do nothing.
        setError("Playback could not start. Click again.");
        stop();
      }
    },
    [audio, encounterId, stop],
  );

  return { playing, playingSegmentId, progress, error, play, stop };
}
