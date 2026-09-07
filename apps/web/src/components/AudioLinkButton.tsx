/**
 * "Get the audio" for one row of the All-notes list.
 *
 * Deliberately does not pre-fetch: `fetchPlaybackUrl` mints a live,
 * playable handle on PHI (a presigned S3 URL, or — on the Drive backend —
 * a link into this server's own session-authenticated streaming proxy).
 * usePassagePlayer.ts and grounding.ts already establish the rule for why
 * that only happens on click, never while a screen loads; a list of many
 * rows is the same rule at a larger scale; minting one per row on load
 * would hand out working links to recordings nobody asked to hear, and
 * write an `encounter.audio.playback_url` audit row for every one of them.
 */
import { useState } from "react";
import { fetchPlaybackUrl } from "../lib/grounding";

export function AudioLinkButton({ encounterId }: { encounterId: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function open() {
    setBusy(true);
    setError(null);
    const result = await fetchPlaybackUrl(encounterId);
    setBusy(false);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    // A new tab, not this one: the link is a direct handle on a
    // consultation recording, and the S3 case is short-lived — losing this
    // tab's place in the list to follow it would cost more than it's worth.
    window.open(result.url, "_blank", "noopener");
  }

  return (
    <span className="audio-link">
      <button type="button" className="ghost" disabled={busy} onClick={() => void open()}>
        {busy ? "Getting audio…" : "Audio"}
      </button>
      {error && <span className="bad">{error}</span>}
    </span>
  );
}
