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
    // "generated" falls through to here — and belongs on the same color as
    // the rest, not on its own. A freshly generated note is the exact same
    // real-world moment encounterTab() above already calls "done" (pipeline_
    // status "note_generated"): the AI has finished, nothing is pending on
    // the system, and it's the doctor's turn. Coloring it "progress" here
    // would put it back on Home's "still working, nothing to do yet" color
    // for the identical fact — the one thing FolderTab.tsx's own docs say
    // this shared vocabulary exists to prevent (`/impeccable critique`,
    // found on the first new screen built after AllNotes.tsx shipped).
    default:
      return "done";
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

// "generated" reads "Ready to review" rather than "Drafted" on purpose — it
// is the exact same real-world moment PIPELINE_LABEL.note_generated already
// names above, and every screen that shows a note's status must say the
// same thing about it (`/impeccable critique`). Originally local to
// AllNotes.tsx; shared here once NoteReview needed the identical label for
// the identical status, found sweeping the app with `/frontend-design`
// polish — NoteReview was the one screen still showing this status as a
// bare enum word instead of the app's own folder-tab language.
export const NOTE_STATUS_LABEL: Record<string, string> = {
  generated: "Ready to review",
  filed: "Filed",
  authenticated: "Authenticated",
  signed: "Signed",
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
