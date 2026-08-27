/**
 * RecordingSession — the recorder proper.
 *
 * Everything load-bearing here was learned from the capture harness
 * (`docs/experiments/audio-capture-harness.html`) rather than assumed, and
 * decision 0024 records the measurements:
 *
 * - **The wake lock must be re-acquired on every return to visible.** The
 *   browser auto-releases it on hide and never restores it. In the harness
 *   run it was acquired at 0.0s, dropped at 35.1s on the first
 *   backgrounding, and never came back — so idle sleep went unguarded for
 *   28 of 29 minutes, and a suspend duly followed at 122.8s.
 * - **An explicit `audioBitsPerSecond` is mandatory.** Without it the
 *   browser picks, which is how the harness recorded 129 kbps stereo
 *   (26.7 MB / 29 min) despite requesting mono.
 * - **A requested constraint is not an achieved setting.** The device
 *   reported `channelCount: 2` while being asked for 1. Mismatches are
 *   surfaced, not assumed away.
 * - **Gaps must be detected, not hoped against.** An AudioWorklet counts
 *   samples; the count only falls behind if the audio graph actually
 *   stalled. Lid close cost 6.5s of real audio and no client architecture
 *   can prevent it — so the honest response is to *record that a gap
 *   happened* and carry it forward, rather than presenting truncated audio
 *   as complete. A silent gap in a clinical record is the failure mode the
 *   PRD explicitly rejects.
 *
 * Plaintext audio never touches disk: each chunk is AES-GCM encrypted
 * before it reaches IndexedDB (crypto.ts, store.ts).
 */
import {
  AUDIO_CONSTRAINTS,
  CHUNK_INTERVAL_MS,
  TARGET_BITS_PER_SECOND,
  assertAudioSettings,
  pickMimeType,
  type AudioSettingsMismatch,
} from "../audio-config";
import { encryptChunk, getAudioKey } from "./crypto";
import { appendChunk } from "./store";

/** Worklet source, inlined so there is no separate asset to ship or 404 on. */
const SAMPLE_COUNTER_WORKLET = [
  "class SampleCounter extends AudioWorkletProcessor {",
  "  constructor() { super(); this.n = 0; this.lastPost = 0; }",
  "  process(inputs) {",
  "    const ch = inputs[0] && inputs[0][0];",
  "    if (ch) { this.n += ch.length; }",
  "    if (currentFrame - this.lastPost >= sampleRate / 4) {",
  "      this.lastPost = currentFrame;",
  "      this.port.postMessage({ samples: this.n });",
  "    }",
  "    return true;",
  "  }",
  "}",
  'registerProcessor("sample-counter", SampleCounter);',
].join("\n");

/** A stretch of wall-clock time during which the audio graph was not running. */
export type AudioGap = {
  atMs: number;
  durationMs: number;
  cause: "suspend" | "stall";
};

export type RecordingStatus = "idle" | "starting" | "recording" | "stopping" | "stopped" | "error";

export type RecordingState = {
  status: RecordingStatus;
  /** Wall clock since start. */
  elapsedMs: number;
  /** Audio actually captured, from the worklet's sample count. */
  capturedMs: number;
  /** elapsedMs - capturedMs: audio that does not exist. */
  missingMs: number;
  chunkCount: number;
  bytes: number;
  gaps: AudioGap[];
  mismatches: AudioSettingsMismatch[];
  mimeType: string | null;
  sampleRate: number | null;
  deviceLabel: string | null;
  error: string | null;
};

const EMPTY_STATE: RecordingState = {
  status: "idle",
  elapsedMs: 0,
  capturedMs: 0,
  missingMs: 0,
  chunkCount: 0,
  bytes: 0,
  gaps: [],
  mismatches: [],
  mimeType: null,
  sampleRate: null,
  deviceLabel: null,
  error: null,
};

/**
 * A wall-clock jump this much larger than expected means the machine was
 * suspended (lid close / sleep). The harness detected exactly one, of 5.8s.
 */
const SUSPEND_JUMP_THRESHOLD_MS = 3000;
/** The worklet posts every ~250ms; silence beyond this means it stalled. */
const STALL_THRESHOLD_MS = 1500;
const MONITOR_INTERVAL_MS = 500;

export class RecordingSession {
  readonly sessionId: string;

  private stream: MediaStream | null = null;
  private recorder: MediaRecorder | null = null;
  private audioContext: AudioContext | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private wakeLock: WakeLockSentinel | null = null;
  private key: CryptoKey | null = null;

  private startedAt = 0;
  private baseWall = 0;
  private baseSamples = 0;
  private samples = 0;
  private sampleRate = 0;
  private lastWorkletMsgAt = 0;
  private lastMonitorAt = 0;
  private monitorTimer: ReturnType<typeof setInterval> | undefined;
  private seq = 0;
  private pendingWrites: Promise<unknown>[] = [];

  private state: RecordingState = { ...EMPTY_STATE };
  private listeners = new Set<(state: RecordingState) => void>();

  private onVisibility = () => {
    if (!document.hidden && this.state.status === "recording") {
      // The browser dropped the wake lock when we were hidden and will not
      // restore it. This single line is the harness's most consequential
      // finding — see the module docstring.
      void this.acquireWakeLock();
    }
  };

  constructor(sessionId: string) {
    this.sessionId = sessionId;
  }

  subscribe(listener: (state: RecordingState) => void): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  getState(): RecordingState {
    return this.state;
  }

  private emit(patch: Partial<RecordingState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of this.listeners) listener(this.state);
  }

  private async acquireWakeLock(): Promise<void> {
    if (!("wakeLock" in navigator)) return;
    if (this.wakeLock && !this.wakeLock.released) return;
    try {
      this.wakeLock = await navigator.wakeLock.request("screen");
    } catch {
      // Refused (page not visible, policy, unsupported). Not fatal — a
      // wake lock only blocks *idle* sleep, and cannot block a lid close
      // regardless, so recording proceeds without it.
    }
  }

  async start(): Promise<void> {
    if (this.state.status === "recording" || this.state.status === "starting") return;
    this.emit({ ...EMPTY_STATE, status: "starting" });

    try {
      this.key = await getAudioKey();

      this.stream = await navigator.mediaDevices.getUserMedia({ audio: AUDIO_CONSTRAINTS });
      const track = this.stream.getAudioTracks()[0];
      const mismatches = assertAudioSettings(track);

      track.addEventListener("ended", () => {
        // The input died (device unplugged, permission revoked). Stop
        // rather than keep a recorder running against nothing.
        this.emit({ error: "The microphone stopped working. Recording ended." });
        void this.stop();
      });

      await this.setupSampleCounter();

      const mimeType = pickMimeType();
      this.recorder = new MediaRecorder(this.stream, {
        ...(mimeType ? { mimeType } : {}),
        // Explicit, never inferred — see the module docstring.
        audioBitsPerSecond: TARGET_BITS_PER_SECOND,
      });
      this.recorder.ondataavailable = (event) => this.handleChunk(event.data);
      this.recorder.onerror = () => this.emit({ status: "error", error: "The recorder failed." });
      this.recorder.start(CHUNK_INTERVAL_MS);

      this.startedAt = Date.now();
      this.lastMonitorAt = this.startedAt;
      document.addEventListener("visibilitychange", this.onVisibility);
      await this.acquireWakeLock();
      this.monitorTimer = setInterval(() => this.monitor(), MONITOR_INTERVAL_MS);

      this.emit({
        status: "recording",
        mismatches,
        mimeType: this.recorder.mimeType,
        sampleRate: this.sampleRate,
        deviceLabel: track.label || null,
      });
    } catch (error) {
      const message =
        error instanceof DOMException && error.name === "NotAllowedError"
          ? "Microphone access was denied. Recording cannot start."
          : error instanceof DOMException && error.name === "NotFoundError"
            ? "No microphone was found on this laptop."
            : `Could not start recording: ${(error as Error).message}`;
      await this.teardown();
      this.emit({ status: "error", error: message });
      throw error;
    }
  }

  private async setupSampleCounter(): Promise<void> {
    const AudioCtor = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    this.audioContext = new AudioCtor();
    this.sampleRate = this.audioContext.sampleRate;

    const url = URL.createObjectURL(
      new Blob([SAMPLE_COUNTER_WORKLET], { type: "text/javascript" }),
    );
    try {
      await this.audioContext.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }

    this.workletNode = new AudioWorkletNode(this.audioContext, "sample-counter");
    this.workletNode.port.onmessage = (event: MessageEvent<{ samples: number }>) => {
      const now = Date.now();
      this.samples = event.data.samples;
      this.lastWorkletMsgAt = now;
      if (!this.baseWall && this.startedAt) {
        // Anchor here, not at start(): the gap between the click and the
        // graph delivering its first quantum is startup latency (~700ms-1.3s
        // measured), not lost audio. Charging it to "missing" was a real bug
        // in the harness before this fix.
        this.baseWall = now;
        this.baseSamples = event.data.samples;
      }
    };

    // gain 0 -> destination keeps the graph scheduled without echoing the
    // room back through the laptop speakers.
    const mute = this.audioContext.createGain();
    mute.gain.value = 0;
    this.audioContext
      .createMediaStreamSource(this.stream!)
      .connect(this.workletNode)
      .connect(mute)
      .connect(this.audioContext.destination);

    if (this.audioContext.state === "suspended") await this.audioContext.resume();
  }

  private handleChunk(data: Blob): void {
    if (data.size === 0) return;
    const offsetMs = Date.now() - this.startedAt;
    const seq = this.seq++;

    // Encrypt-then-store, off the critical path but tracked so stop() can
    // await it. Dropping a chunk because stop() raced the write would lose
    // the tail of a consultation.
    const write = (async () => {
      try {
        const plaintext = await data.arrayBuffer();
        const { ciphertext, iv } = await encryptChunk(this.key!, plaintext);
        await appendChunk({
          sessionId: this.sessionId,
          seq,
          offsetMs,
          byteLength: plaintext.byteLength,
          ciphertext,
          iv,
          mimeType: this.recorder?.mimeType ?? "audio/webm",
        });
        this.emit({
          chunkCount: this.state.chunkCount + 1,
          bytes: this.state.bytes + plaintext.byteLength,
        });
      } catch (error) {
        // A failed write is data loss and must be visible. The PRD rejects
        // a silent gap in the record, and this is one.
        this.emit({
          error: `A piece of the recording could not be saved to this laptop: ${(error as Error).message}`,
        });
      }
    })();
    this.pendingWrites.push(write);
  }

  /** Runs every 500ms: detects suspends and stalls, updates the counters. */
  private monitor(): void {
    const now = Date.now();
    const gaps = [...this.state.gaps];

    const jump = now - this.lastMonitorAt - MONITOR_INTERVAL_MS;
    if (jump > SUSPEND_JUMP_THRESHOLD_MS) {
      // Wall clock moved much further than our own timer did: the machine
      // was suspended. Audio in that window does not exist, and no amount
      // of client architecture could have kept it — record it and move on.
      gaps.push({ atMs: this.lastMonitorAt - this.startedAt, durationMs: jump, cause: "suspend" });
    }
    this.lastMonitorAt = now;

    const stale = this.lastWorkletMsgAt ? now - this.lastWorkletMsgAt : 0;
    if (stale > STALL_THRESHOLD_MS) {
      const last = gaps[gaps.length - 1];
      // Coalesce: one ongoing stall is one gap, not one per monitor tick.
      if (last && last.cause === "stall" && last.atMs + last.durationMs >= stale - MONITOR_INTERVAL_MS * 3) {
        last.durationMs = stale;
      } else {
        gaps.push({ atMs: this.lastWorkletMsgAt - this.startedAt, durationMs: stale, cause: "stall" });
      }
    }

    const elapsedMs = now - this.startedAt;
    const capturedMs =
      this.sampleRate && this.baseWall
        ? ((this.samples - this.baseSamples) / this.sampleRate) * 1000
        : 0;
    const measuredWindow = this.baseWall ? this.lastWorkletMsgAt - this.baseWall : 0;

    this.emit({
      elapsedMs,
      capturedMs,
      // Measured between the two points sampled at the same instant, so the
      // worklet's 250ms post interval does not read as loss.
      missingMs: Math.max(0, measuredWindow - capturedMs),
      gaps,
    });
  }

  async stop(): Promise<{ chunkCount: number; missingMs: number; gaps: AudioGap[] }> {
    if (this.state.status !== "recording" && this.state.status !== "error") {
      return { chunkCount: this.state.chunkCount, missingMs: this.state.missingMs, gaps: this.state.gaps };
    }
    this.emit({ status: "stopping" });

    // requestData() flushes the partial chunk MediaRecorder is holding.
    // Without it the final few seconds of a consultation are discarded —
    // the tail, which is often where the plan is stated.
    try {
      if (this.recorder && this.recorder.state === "recording") {
        this.recorder.requestData();
        await new Promise<void>((resolve) => {
          this.recorder!.addEventListener("stop", () => resolve(), { once: true });
          this.recorder!.stop();
        });
      }
    } catch {
      // Already inactive; the pending writes below are still awaited.
    }

    await Promise.allSettled(this.pendingWrites);
    this.monitor();
    await this.teardown();
    this.emit({ status: "stopped" });

    return {
      chunkCount: this.state.chunkCount,
      missingMs: this.state.missingMs,
      gaps: this.state.gaps,
    };
  }

  private async teardown(): Promise<void> {
    clearInterval(this.monitorTimer);
    this.monitorTimer = undefined;
    document.removeEventListener("visibilitychange", this.onVisibility);

    try {
      await this.wakeLock?.release();
    } catch {
      /* already released */
    }
    this.wakeLock = null;

    this.workletNode?.port.close();
    this.workletNode?.disconnect();
    this.workletNode = null;

    try {
      await this.audioContext?.close();
    } catch {
      /* already closed */
    }
    this.audioContext = null;

    // Releasing the tracks is what turns off the browser's recording
    // indicator. Leaving them live would tell a doctor the room is still
    // being recorded when it is not — or worse, the reverse.
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    this.recorder = null;
  }
}
