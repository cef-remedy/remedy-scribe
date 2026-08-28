/**
 * Grounding (Phase 3, P0-7) — "where did this line come from?"
 *
 * This is the product's trust mechanism. The doctor's rational response to
 * "an AI wrote this" is "prove it," and this is the proof. Which means the
 * one unacceptable outcome is a *confident wrong answer*: a highlight over
 * the wrong words, or a play button that does nothing.
 *
 * Two rules follow from that, and both are enforced here rather than left to
 * the components:
 *
 * 1. **Never highlight by stale offsets.** The server tells us whether a
 *    section's stored offsets still delimit its current text (`spans_fit`).
 *    When they don't, grounding for that section is *withdrawn*, not
 *    approximated. `groundableLines` returns nothing rather than something
 *    plausible.
 *
 * 2. **Never offer playback we cannot deliver.** The audio state is resolved
 *    server-side against object storage, not against the database, because a
 *    bucket lifecycle rule deletes recordings with nothing updating the row.
 *    `audioNotice` turns each state into words for the doctor.
 */
import { api, OfflineError } from "../api/client";

export type GroundedSegment = {
  id: string;
  index: number;
  speaker: string;
  text: string;
  start_ms: number | null;
  end_ms: number | null;
  /** False for a neighbour included only as context — never presented as evidence. */
  cited: boolean;
};

export type GroundedSpan = {
  text_start: number;
  text_end: number;
  segment_ids: string[];
  text: string;
};

export type GroundedSection = {
  suppressed: boolean;
  spans: GroundedSpan[];
  spans_fit: boolean;
  edited_since_generation: boolean;
};

export type AudioState = "available" | "never_recorded" | "withdrawn" | "expired" | "unreachable";
export type TranscriptState = "available" | "never_transcribed" | "withdrawn" | "expired";

export type Grounding = {
  note_id: string;
  encounter_id: string;
  audio_state: AudioState;
  transcript_state: TranscriptState;
  segments: GroundedSegment[];
  sections: Record<string, GroundedSection>;
};

export async function fetchGrounding(noteId: string): Promise<Grounding | null> {
  try {
    const { data, error } = await api.GET("/api/v1/notes/{note_id}/grounding", {
      params: { path: { note_id: noteId } },
    });
    if (error || !data) return null;
    return data as unknown as Grounding;
  } catch {
    // Offline or unreachable. Grounding is additive to the review screen —
    // losing it must not stop a doctor reading and signing a note.
    return null;
  }
}

export type PlaybackUrl = { url: string; expiresInSeconds: number };

/**
 * Minted on demand, one click at a time. The URL is a live playable handle on
 * PHI, so it is deliberately not fetched when the note loads — only when the
 * doctor actually asks to hear a passage.
 *
 * A 409 means the recording is genuinely gone and carries the reason with it.
 * That reason is shown verbatim: "no audio" without a reason is the dead play
 * button this phase exists to avoid.
 */
export async function fetchPlaybackUrl(encounterId: string): Promise<PlaybackUrl | { error: string }> {
  try {
    const { data, error, response } = await api.GET("/api/v1/encounters/{encounter_id}/audio-url", {
      params: { path: { encounter_id: encounterId } },
    });
    if (error || !data) {
      const detail = (error as { detail?: string } | undefined)?.detail;
      if (response.status === 409) return { error: detail ?? "The recording is no longer available." };
      return { error: "Could not get the recording just now." };
    }
    const out = data as { url: string; expires_in_seconds: number };
    return { url: out.url, expiresInSeconds: out.expires_in_seconds };
  } catch (e) {
    return { error: e instanceof OfflineError ? "No connection — audio cannot be played." : "Could not play audio." };
  }
}

/** One clickable line of the note, with the passages it cites. */
export type GroundedLine = {
  key: string;
  text: string;
  segmentIds: string[];
};

/**
 * The lines of a section that can be interrogated.
 *
 * Returns `[]` — meaning "render this section as plain text, offer nothing" —
 * whenever grounding would be a guess: no grounding loaded, a suppressed
 * section, or offsets that no longer fit the text. That last case is the
 * important one: after an edit shifts the offsets, slicing by them still
 * *works*, it just highlights the wrong words, which is worse than not
 * offering the feature.
 */
export function groundableLines(section: GroundedSection | undefined): GroundedLine[] {
  if (!section || section.suppressed || !section.spans_fit) return [];
  return section.spans.map((span, i) => ({
    key: `${span.text_start}-${span.text_end}-${i}`,
    text: span.text,
    segmentIds: span.segment_ids,
  }));
}

/**
 * The passages a line cites, plus their immediate neighbours for context,
 * in transcript order.
 *
 * The neighbours are kept and flagged rather than dropped: a passage read
 * without its surroundings is easy to misinterpret, but a neighbour is not
 * what the line cited and must not be shown as though it were.
 */
export function passagesForLine(grounding: Grounding, segmentIds: string[]): GroundedSegment[] {
  if (segmentIds.length === 0) return [];
  const cited = new Set(segmentIds);
  const indices = grounding.segments.filter((s) => cited.has(s.id)).map((s) => s.index);
  if (indices.length === 0) return [];
  const lo = Math.min(...indices) - 1;
  const hi = Math.max(...indices) + 1;
  return grounding.segments
    .filter((s) => s.index >= lo && s.index <= hi)
    .map((s) => ({ ...s, cited: cited.has(s.id) }))
    .sort((a, b) => a.index - b.index);
}

/**
 * The degradation ladder in words. The Phase 3 heads-up is explicit that the
 * doctor should understand *which* state they are in — a disabled control with
 * no explanation is the failure, not the fix.
 */
export function audioNotice(state: AudioState, transcript: TranscriptState): string | null {
  if (transcript === "withdrawn") {
    // The bottom rung, but with the reason that matters. A withdrawal is a
    // legal event under P0-1, not the passage of time, and the retention
    // purge deletes the transcript alongside the audio.
    return "The patient withdrew consent, so both the recording and the transcript were deleted. This note remains a permanent record, but there is no longer a source to check it against.";
  }
  if (transcript !== "available") {
    return "The source transcript has passed its retention period and been deleted. This note is a permanent record, but its source is no longer available to check against.";
  }
  switch (state) {
    case "available":
      return null;
    case "never_recorded":
      return "No recording was uploaded for this consultation. Transcript passages can still be checked; there is no audio to play.";
    case "withdrawn":
      return "The recording was deleted at the patient's request. Transcript passages can still be checked; the audio cannot be played.";
    case "expired":
      return "The recording's retention period has elapsed and the audio has been deleted. Transcript passages can still be checked; the audio cannot be played.";
    case "unreachable":
      return "Audio storage could not be reached. This is not a deletion — playback may work again shortly.";
  }
}

export function formatTimestamp(ms: number | null): string {
  if (ms === null) return "—";
  const total = Math.floor(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
