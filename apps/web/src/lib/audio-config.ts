/**
 * Audio capture settings — the user's call, recorded as decision 0025.
 *
 * Mono Opus at 32 kbps. Consumed by Phase 2.2's recorder; defined here now
 * because the first harness run on real hardware recorded **129 kbps
 * stereo — 26.7 MB for 29 minutes**, having silently ignored a
 * `channelCount: 1` constraint. Over a clinic day that is roughly 550 MB
 * up over clinic wifi instead of ~130 MB, for no clinical benefit: Whisper
 * transcribes 16 kHz mono speech perfectly well, and the second channel of
 * a single room mic is near-duplicate information.
 *
 * Do not trust the constraint alone. `getUserMedia` may hand back stereo
 * regardless (it did on the test laptop), so the recorder must also set an
 * explicit bitrate on MediaRecorder and downmix rather than assuming the
 * request was honoured. `assertAudioSettings` exists to make that mismatch
 * loud instead of silent.
 */

export const TARGET_BITS_PER_SECOND = 32_000;
export const TARGET_CHANNEL_COUNT = 1;
/** 16 kHz is ample for speech ASR; browsers commonly ignore this and give 48 kHz. */
export const PREFERRED_SAMPLE_RATE = 16_000;

/** Chunk length for MediaRecorder and the IndexedDB write-ahead queue. */
export const CHUNK_INTERVAL_MS = 5_000;

/**
 * Ordered by preference, feature-detected rather than hardcoded: Chrome,
 * Edge and Firefox give WebM/Opus; Safari was MP4/AAC-only before 18.4.
 * Groq Whisper accepts both, so the only wrong move is assuming one.
 */
export const CANDIDATE_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/ogg;codecs=opus",
  "audio/webm",
  "audio/mp4;codecs=mp4a.40.2",
  "audio/mp4",
] as const;

export function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  for (const mime of CANDIDATE_MIME_TYPES) {
    try {
      if (MediaRecorder.isTypeSupported(mime)) return mime;
    } catch {
      // isTypeSupported can throw on malformed input in some engines.
    }
  }
  return undefined; // let the browser choose, and report what it chose
}

export const AUDIO_CONSTRAINTS: MediaTrackConstraints = {
  channelCount: TARGET_CHANNEL_COUNT,
  sampleRate: PREFERRED_SAMPLE_RATE,
  // Left off deliberately: these are tuned for intelligible conference
  // calls, not for faithful transcription. Aggressive noise suppression can
  // clip the start of quiet speech, which for Taglish code-switching is
  // exactly where accuracy is already weakest.
  echoCancellation: false,
  noiseSuppression: false,
  autoGainControl: false,
};

export type AudioSettingsMismatch = {
  field: string;
  requested: number | string;
  actual: number | string;
};

/**
 * Compares what we asked for against what the device actually gave.
 * Returns the mismatches instead of throwing: a stereo track is worth
 * logging loudly and correcting downstream, not worth refusing to record a
 * consultation over.
 */
export function assertAudioSettings(track: MediaStreamTrack): AudioSettingsMismatch[] {
  const actual = track.getSettings();
  const mismatches: AudioSettingsMismatch[] = [];

  if (actual.channelCount !== undefined && actual.channelCount !== TARGET_CHANNEL_COUNT) {
    mismatches.push({
      field: "channelCount",
      requested: TARGET_CHANNEL_COUNT,
      actual: actual.channelCount,
    });
  }
  if (actual.sampleRate !== undefined && actual.sampleRate !== PREFERRED_SAMPLE_RATE) {
    mismatches.push({
      field: "sampleRate",
      requested: PREFERRED_SAMPLE_RATE,
      actual: actual.sampleRate,
    });
  }
  return mismatches;
}

/** Rough upload size, for the queue UI and for sanity-checking the bitrate. */
export function estimatedBytesPerMinute(): number {
  return (TARGET_BITS_PER_SECOND / 8) * 60;
}
