/**
 * P0-1: "a persistent recording indicator remains visible for the duration."
 *
 * Deliberately hard to miss and impossible to dismiss. This is a legal
 * control under RA 4200, not a status widget — a patient in the room must be
 * able to tell at a glance that they are being recorded, and the doctor must
 * never be able to hide it. `role="status"` with `aria-live` so it is
 * announced rather than only seen.
 *
 * Note it deliberately renders while *paused* too, in a visually distinct
 * state: "the app is holding a recording session" is the fact that matters
 * for consent, and a paused session that looks identical to no session is
 * exactly how someone gets recorded without realising it.
 */
import { formatDuration } from "../lib/format";

export function RecordingIndicator({
  active,
  elapsedMs,
  missingMs,
}: {
  active: boolean;
  elapsedMs: number;
  missingMs: number;
}) {
  if (!active) return null;

  // Surfaced in the indicator itself, not buried in a details panel: if
  // audio was lost, the doctor needs to know before they rely on the note.
  const hasGap = missingMs >= 1000;

  return (
    <div className="rec-indicator" role="status" aria-live="polite">
      <span className="rec-dot" aria-hidden="true" />
      <strong>Recording</strong>
      <span className="rec-time">{formatDuration(elapsedMs)}</span>
      {hasGap && (
        <span className="rec-gap" title="Audio was lost during a system sleep or stall">
          {formatDuration(missingMs)} missing
        </span>
      )}
    </div>
  );
}
