/**
 * One note section, readable as evidence before it is editable as text
 * (Phase 3, P0-7).
 *
 * The tension this component resolves: 2.6 made editing frictionless (a
 * textarea, saved on blur), but you cannot click a line *inside* a textarea
 * to ask where it came from. So the section renders as clickable lines by
 * default and swaps to the textarea when the doctor chooses to edit.
 *
 * That ordering is the point, not a workaround. The first pass over an
 * AI-drafted note should be verification, and APSO already puts the
 * assessment first for the same reason. Making "check this line" the default
 * gesture and "rewrite it" the deliberate one matches what the doctor is
 * accountable for when they sign.
 *
 * Interaction is the two-tap the checklist specifies: **tap a line to
 * highlight its source passage, tap it again to hear it.** Two taps rather
 * than one because playing audio out loud in a consultation room is not
 * something to trigger by accident.
 */
import { useState } from "react";
import type { Grounding } from "../lib/grounding";
import { formatTimestamp, groundableLines, passagesForLine } from "../lib/grounding";
import type { PassagePlayer } from "../lib/usePassagePlayer";

type Props = {
  sectionKey: string;
  label: string;
  hint: string;
  text: string;
  /** The note's saved text, for the unsaved-edit notice. */
  savedText: string;
  signed: boolean;
  saving: boolean;
  grounding: Grounding | null;
  player: PassagePlayer;
  onChange: (text: string) => void;
  onBlur: () => void;
};

export function GroundedSection({
  sectionKey,
  label,
  hint,
  text,
  savedText,
  signed,
  saving,
  grounding,
  player,
  onChange,
  onBlur,
}: Props) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  const section = grounding?.sections[sectionKey];
  const lines = groundableLines(section);
  const canGround = grounding !== null && lines.length > 0;
  // A signed note is a permanent record: there is nothing to edit, and the
  // only useful thing left to do with it is check it.
  const showEditor = !signed && (editing || !canGround);

  const selected = lines.find((l) => l.key === selectedKey) ?? null;
  const passages = selected && grounding ? passagesForLine(grounding, selected.segmentIds) : [];

  const onLineClick = (key: string, segmentIds: string[]) => {
    if (key !== selectedKey) {
      // First tap: highlight the source. Nothing is played yet.
      setSelectedKey(key);
      player.stop();
      return;
    }
    // Second tap on the same line: play from the first cited passage.
    if (!grounding) return;
    const cited = passagesForLine(grounding, segmentIds).filter((p) => p.cited);
    const playable = cited.find((p) => p.start_ms !== null && p.end_ms !== null);
    if (playable) void player.play(playable.id, playable.start_ms!, playable.end_ms!);
  };

  return (
    <section className="card">
      <h2>{label}</h2>
      <p className="muted">{hint}</p>

      {showEditor ? (
        <>
          <textarea
            aria-label={label}
            rows={5}
            disabled={signed}
            value={text}
            onChange={(e) => onChange(e.target.value)}
            onBlur={() => {
              onBlur();
              setEditing(false);
            }}
          />
          {saving && <p className="muted">Saving…</p>}
          {!signed && text !== savedText && (
            <p className="muted">Unsaved — click outside the box to save this edit.</p>
          )}
          {!signed && canGround && (
            <button type="button" className="ghost" onClick={() => setEditing(false)}>
              Done editing
            </button>
          )}
        </>
      ) : (
        <>
          <p className="grounded-text" data-testid={`grounded-${sectionKey}`}>
            {lines.map((line) => (
              <button
                key={line.key}
                type="button"
                className={[
                  "ground-line",
                  line.key === selectedKey ? "is-selected" : "",
                  line.segmentIds.length === 0 ? "is-uncited" : "",
                ]
                  .join(" ")
                  .trim()}
                aria-pressed={line.key === selectedKey}
                onClick={() => onLineClick(line.key, line.segmentIds)}
                title={
                  line.segmentIds.length === 0
                    ? "This line cites no transcript passage"
                    : line.key === selectedKey
                      ? "Click again to hear this passage"
                      : "Click to see where this came from"
                }
              >
                {line.text}
              </button>
            ))}
          </p>
          {!signed && (
            <button type="button" className="ghost" onClick={() => setEditing(true)}>
              Edit this section
            </button>
          )}
        </>
      )}

      {/* Why grounding is not on offer, when it isn't. Silence here reads as
          "this note has no sources", which is a different and worse claim. */}
      {!showEditor && section?.suppressed && (
        <p className="muted">
          Left blank on purpose — the recording had nothing clear enough here to draft from. Add
          anything that belongs in this section yourself.
        </p>
      )}
      {section && !section.spans_fit && !section.suppressed && text.trim() !== "" && (
        <p className="ground-stale">
          Source links for this section no longer line up with the text — they were recorded against
          the original draft and this section has since changed. Not shown rather than shown
          approximately.
        </p>
      )}
      {section?.edited_since_generation && section.spans_fit && (
        <p className="muted">
          Edited since drafting. The passages below are what the <em>draft</em> cited; wording you
          have changed is yours, not the recording's.
        </p>
      )}

      {selected && (
        <div className="passages">
          <h3>
            Source {selected.segmentIds.length === 0 ? "— none cited" : "transcript"}
            {player.playing && <span className="playing-dot" aria-label="playing" />}
          </h3>
          {/* The chase-light playhead: real playback position through the
              cited window, not a generic spinner. */}
          {player.playing && (
            <div className="playhead-track" aria-hidden="true">
              <div className="playhead-fill" style={{ transform: `scaleX(${player.progress})` }} />
            </div>
          )}
          {selected.segmentIds.length === 0 ? (
            <p className="ground-stale">
              This line cites no transcript passage. Nothing in the recording was linked to it, so
              treat it as unverified and check it against what you remember of the consultation.
            </p>
          ) : passages.length === 0 ? (
            <p className="muted">
              The cited passage is no longer available — the transcript has passed its retention
              period.
            </p>
          ) : (
            <ol className="passage-list">
              {passages.map((p) => (
                <li key={p.id} className={p.cited ? "is-cited" : "is-context"}>
                  <div className="passage-head">
                    <span className="passage-speaker">{p.speaker}</span>
                    <span className="passage-time">{formatTimestamp(p.start_ms)}</span>
                    {!p.cited && <span className="passage-tag">context</span>}
                    {player.playingSegmentId === p.id && <span className="passage-tag">playing</span>}
                  </div>
                  <p className="passage-text">{p.text}</p>
                </li>
              ))}
            </ol>
          )}
          {player.error && <p className="ground-stale">{player.error}</p>}
          {player.playing && (
            <button type="button" className="ghost" onClick={player.stop}>
              Stop
            </button>
          )}
        </div>
      )}
    </section>
  );
}
