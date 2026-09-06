/**
 * Maps every status vocabulary in this app — encounter pipeline_status,
 * note status, and the local upload queue's own states — onto one shared
 * four-color tab family (The Patient Folder direction, apps/web/index.html).
 *
 * One mapping function per vocabulary, not one giant lookup table: the
 * three status enums are genuinely different types with different values,
 * and merging them into a single object invites a typo that silently falls
 * through to a default color instead of a type error.
 */

export type TabKind = "progress" | "done" | "attention" | "hold" | "blank";

export function encounterTab(pipelineStatus: string): TabKind {
  switch (pipelineStatus) {
    case "transcription_failed":
    case "generation_failed":
      return "attention";
    case "blocked_no_consent":
      return "hold";
    case "note_generated":
      return "done";
    // recording, uploaded, transcribed, and anything not yet named above —
    // the system is still working, nothing for the doctor to act on yet.
    default:
      return "progress";
  }
}

export function noteTab(noteStatus: string): TabKind {
  switch (noteStatus) {
    case "signed":
    case "filed":
    case "authenticated":
      return "done";
    default:
      return "progress"; // generated: drafted, waiting on the doctor
  }
}

export function queueTab(state: string): TabKind {
  switch (state) {
    case "failed":
      return "attention";
    case "abandoned":
      return "hold";
    case "uploaded":
    case "confirmed":
    case "done":
      return "done";
    default:
      return "progress"; // recording, pending, uploading
  }
}

/** Human label for a folder tab — short, uppercase by CSS, not by the string. */
export const PIPELINE_LABEL: Record<string, string> = {
  recording: "Recording",
  uploaded: "Uploaded",
  transcribed: "Transcribing",
  note_generated: "Ready to review",
  transcription_failed: "Transcription failed",
  generation_failed: "Note generation failed",
  blocked_no_consent: "No consent on file",
};

/**
 * The lockable one-way step-sequence (raised into this direction from the
 * roll's declined origami-fold candidate): recording → uploaded →
 * transcribed → note_generated → signed, each stage passed and irreversible.
 * A terminal failure or hold state stops the sequence rather than faking a
 * position within it.
 */
const SEQUENCE = ["recording", "uploaded", "transcribed", "note_generated", "signed"] as const;

export function sequencePosition(pipelineStatus: string, noteSigned: boolean): {
  index: number;
  terminal: "attention" | "hold" | null;
} {
  if (pipelineStatus === "transcription_failed" || pipelineStatus === "generation_failed") {
    return { index: SEQUENCE.indexOf(pipelineStatus === "transcription_failed" ? "uploaded" : "transcribed"), terminal: "attention" };
  }
  if (pipelineStatus === "blocked_no_consent") {
    return { index: 0, terminal: "hold" };
  }
  if (noteSigned) return { index: SEQUENCE.length - 1, terminal: null };
  const at = SEQUENCE.indexOf(pipelineStatus as (typeof SEQUENCE)[number]);
  return { index: at === -1 ? 0 : at, terminal: null };
}

export const SEQUENCE_LENGTH = SEQUENCE.length;
