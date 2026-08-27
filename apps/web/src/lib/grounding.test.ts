/**
 * Phase 3: the client half of grounding (P0-7).
 *
 * These cover the decisions that keep the UI from making a confident wrong
 * claim — withholding grounding rather than approximating it, ranking a
 * context passage below a cited one, and turning each rung of the
 * audio-degradation ladder into words rather than a disabled control.
 */
import { describe, expect, it } from "vitest";
import {
  audioNotice,
  formatTimestamp,
  groundableLines,
  passagesForLine,
  type GroundedSection,
  type Grounding,
} from "./grounding";

function section(overrides: Partial<GroundedSection> = {}): GroundedSection {
  return {
    suppressed: false,
    spans_fit: true,
    edited_since_generation: false,
    spans: [
      { text_start: 0, text_end: 36, segment_ids: ["seg2"], text: "Likely community-acquired pneumonia." },
      { text_start: 37, text_end: 63, segment_ids: ["seg4"], text: "Consider chest radiograph." },
    ],
    ...overrides,
  };
}

function grounding(overrides: Partial<Grounding> = {}): Grounding {
  return {
    note_id: "n1",
    encounter_id: "e1",
    audio_state: "available",
    transcript_state: "available",
    segments: [
      { id: "seg1", index: 1, speaker: "speaker_0", text: "before", start_ms: 5_000, end_ms: 6_000, cited: false },
      { id: "seg2", index: 2, speaker: "speaker_1", text: "the cited bit", start_ms: 10_000, end_ms: 11_400, cited: true },
      { id: "seg3", index: 3, speaker: "speaker_0", text: "after", start_ms: 15_000, end_ms: 16_000, cited: false },
      { id: "seg4", index: 4, speaker: "speaker_1", text: "second cited", start_ms: 20_000, end_ms: 21_000, cited: true },
      { id: "seg5", index: 5, speaker: "speaker_0", text: "trailing", start_ms: 25_000, end_ms: 26_000, cited: false },
    ],
    sections: { assessment: section() },
    ...overrides,
  };
}

describe("groundableLines", () => {
  it("returns one clickable line per cited sentence", () => {
    const lines = groundableLines(section());

    expect(lines.map((l) => l.text)).toEqual([
      "Likely community-acquired pneumonia.",
      "Consider chest radiograph.",
    ]);
    expect(lines[0].segmentIds).toEqual(["seg2"]);
  });

  it("offers nothing when the stored offsets no longer fit the text", () => {
    // The important case. Slicing by stale offsets still *works* — it just
    // highlights the wrong words, which is worse than not offering grounding
    // at all for a feature whose only job is proof.
    expect(groundableLines(section({ spans_fit: false }))).toEqual([]);
  });

  it("offers nothing for a suppressed section", () => {
    expect(groundableLines(section({ suppressed: true, spans: [] }))).toEqual([]);
  });

  it("offers nothing when grounding failed to load", () => {
    expect(groundableLines(undefined)).toEqual([]);
  });

  it("keeps a line that cites nothing, so the UI can flag it", () => {
    const lines = groundableLines(
      section({
        spans: [{ text_start: 0, text_end: 36, segment_ids: [], text: "Likely community-acquired pneumonia." }],
      }),
    );

    expect(lines).toHaveLength(1);
    expect(lines[0].segmentIds).toEqual([]);
  });
});

describe("passagesForLine", () => {
  it("returns the cited passage with one neighbour either side", () => {
    const passages = passagesForLine(grounding(), ["seg2"]);

    expect(passages.map((p) => p.id)).toEqual(["seg1", "seg2", "seg3"]);
  });

  it("marks neighbours as context rather than evidence", () => {
    const passages = passagesForLine(grounding(), ["seg2"]);

    expect(passages.find((p) => p.id === "seg2")!.cited).toBe(true);
    expect(passages.find((p) => p.id === "seg1")!.cited).toBe(false);
    expect(passages.find((p) => p.id === "seg3")!.cited).toBe(false);
  });

  it("does not re-mark a passage as cited just because it appears in the window", () => {
    // seg4 is cited by a *different* line. Rendering it as this line's source
    // would be a fabricated citation.
    const passages = passagesForLine(grounding(), ["seg2"]);

    expect(passages.some((p) => p.id === "seg4" && p.cited)).toBe(false);
  });

  it("spans the whole window when a line cites several passages", () => {
    const passages = passagesForLine(grounding(), ["seg2", "seg4"]);

    expect(passages.map((p) => p.id)).toEqual(["seg1", "seg2", "seg3", "seg4", "seg5"]);
    expect(passages.filter((p) => p.cited).map((p) => p.id)).toEqual(["seg2", "seg4"]);
  });

  it("returns nothing when the line cites nothing", () => {
    expect(passagesForLine(grounding(), [])).toEqual([]);
  });

  it("returns nothing when the cited passages are no longer in the transcript", () => {
    expect(passagesForLine(grounding({ segments: [] }), ["seg2"])).toEqual([]);
  });
});

describe("audioNotice — the degradation ladder in words", () => {
  it("says nothing when audio and transcript are both there", () => {
    expect(audioNotice("available", "available")).toBeNull();
  });

  it("explains that the recording was deleted at the patient's request", () => {
    const notice = audioNotice("withdrawn", "available")!;

    expect(notice).toContain("patient's request");
    // And that the transcript is still checkable — the middle rung, not the bottom.
    expect(notice).toContain("Transcript passages can still be checked");
  });

  it("distinguishes retention expiry from a withdrawal", () => {
    expect(audioNotice("expired", "available")).toContain("retention period");
    expect(audioNotice("expired", "available")).not.toContain("patient's request");
  });

  it("does not present unreachable storage as a deletion", () => {
    // "We could not check" and "it is gone" are different facts, and one of
    // them is permanent.
    expect(audioNotice("unreachable", "available")).toContain("not a deletion");
  });

  it("reports a lost transcript as the bottom rung regardless of audio state", () => {
    const notice = audioNotice("available", "expired")!;

    expect(notice).toContain("no longer available to check against");
  });
});

describe("formatTimestamp", () => {
  it("renders minutes and zero-padded seconds", () => {
    expect(formatTimestamp(0)).toBe("0:00");
    expect(formatTimestamp(9_000)).toBe("0:09");
    expect(formatTimestamp(605_000)).toBe("10:05");
  });

  it("renders a passage with no timing as unknown rather than 0:00", () => {
    // A segment with no words has no timestamp. Showing "0:00" would claim it
    // starts at the beginning of the consultation.
    expect(formatTimestamp(null)).toBe("—");
  });
});
