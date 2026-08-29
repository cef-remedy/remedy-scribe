/**
 * The post-encounter five-star prompt (Phase 6, PRD success metrics).
 *
 * This is the only pilot signal that cannot be computed from data the
 * product already holds — edit burden, documentation time and voluntary use
 * are all derivable, and a doctor's own judgement of whether the thing
 * helped is not. That makes response rate the thing to protect, and it
 * drives every choice here:
 *
 * - **It appears after signing, not before.** Asking mid-workflow buys a
 *   rating at the cost of interrupting the clinical task, and a doctor
 *   rushing past a modal to finish a note gives you a number that means
 *   nothing.
 * - **It is dismissible and never blocks anything.** A prompt that must be
 *   answered trains people to click a star at random to make it go away,
 *   which is worse than no data because it looks like data.
 * - **One click submits.** The comment box only appears once a rating is
 *   given, so the cheap action stays cheap and the expensive one is opt-in.
 *
 * ⚠️ The comment box carries a PHI warning, because it is the one field in
 * the pilot instrumentation a doctor could type a patient's name into. It
 * is encrypted at rest server-side and excluded from the pilot report, but
 * the cheapest place to prevent that data existing is here.
 */
import { useState } from "react";
import { api, OfflineError } from "../api/client";

type Props = {
  encounterId: string;
  onDone?: () => void;
};

const STARS = [1, 2, 3, 4, 5];

export function RatingPrompt({ encounterId, onDone }: Props) {
  const [stars, setStars] = useState<number | null>(null);
  const [comment, setComment] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (value: number, withComment: string) => {
    setError(null);
    try {
      const { error: apiError } = await api.POST("/api/v1/pilot/encounters/{encounter_id}/rating", {
        params: { path: { encounter_id: encounterId } },
        body: { stars: value, comment: withComment.trim() || null },
      });
      if (apiError) {
        setError("That rating could not be saved.");
        return;
      }
      setSubmitted(true);
      onDone?.();
    } catch (e) {
      // Never escalated: a failed rating must not look like a failed
      // signature. The note is already signed and permanent by this point.
      setError(
        e instanceof OfflineError
          ? "No connection — your rating was not saved."
          : "That rating could not be saved.",
      );
    }
  };

  if (dismissed) return null;

  if (submitted) {
    return (
      <section className="card rating">
        <p className="muted">Thanks — that helps us judge whether this is working.</p>
      </section>
    );
  }

  return (
    <section className="card rating">
      <h2>How did that go?</h2>
      <p className="muted">
        One tap, and only if you want to. This is the only part of the pilot we cannot measure
        without asking you.
      </p>

      <div className="stars" role="group" aria-label="Rate this consultation">
        {STARS.map((value) => (
          <button
            key={value}
            type="button"
            className={`star${stars !== null && value <= stars ? " is-on" : ""}`}
            aria-label={`${value} star${value === 1 ? "" : "s"}`}
            aria-pressed={stars === value}
            onClick={() => {
              setStars(value);
              // Submit immediately. The comment below is an optional
              // follow-up, not a second step this rating waits on.
              void submit(value, comment);
            }}
          >
            ★
          </button>
        ))}
      </div>

      {stars !== null && (
        <>
          <label htmlFor="rating-comment">Anything worth saying? (optional)</label>
          <textarea
            id="rating-comment"
            rows={2}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            onBlur={() => stars !== null && comment.trim() && void submit(stars, comment)}
          />
          <p className="muted">
            Please keep patient details out of this box — it is feedback about the tool, not part
            of the medical record.
          </p>
        </>
      )}

      {error && <p className="ground-stale">{error}</p>}

      <button type="button" className="ghost" onClick={() => setDismissed(true)}>
        Not now
      </button>
    </section>
  );
}
