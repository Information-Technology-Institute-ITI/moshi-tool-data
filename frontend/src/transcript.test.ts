import { describe, expect, it } from "vitest";
import {
  addSegment,
  boundsError,
  chronological,
  deleteSegment,
  intersecting,
  joinSegments,
  neighbourAfter,
  splitSegment,
  toggleFlag,
} from "./transcript";
import type { Annotation, Speaker, TranscriptUtterance } from "./types";

function utterance(
  id: string,
  start: number,
  end: number,
  speaker: Speaker,
  text: string,
): TranscriptUtterance {
  return {
    id,
    speaker,
    start_sample: start,
    end_sample: end,
    text,
    model_text: "",
    model_speaker: null,
    quality_flags: [],
    alignment_status: "aligned",
    human_verified: false,
    review_candidates: [],
  };
}

function annotationWith(transcript: TranscriptUtterance[]): Annotation {
  return {
    source_id: "source_1",
    version: 3,
    assistant_speaker: null,
    channel_routing_mode: "mono",
    channel_routing_verified: false,
    speaker_channel_map: {},
    activities_finalized: false,
    activities: [],
    speaker_references: [],
    exclusions: [],
    transcript,
    aligned_words: [],
    note: "",
  };
}

const base = annotationWith([
  utterance("u1", 0, 24_000, "A", "hello there"),
  utterance("u2", 24_000, 48_000, "A", "how are you"),
  utterance("u3", 48_000, 72_000, "B", "quite well"),
]);

describe("ordering and lookup", () => {
  it("sorts chronologically regardless of input order", () => {
    const shuffled = annotationWith([base.transcript[2], base.transcript[0], base.transcript[1]]);
    expect(chronological(shuffled.transcript).map((item) => item.id)).toEqual([
      "u1",
      "u2",
      "u3",
    ]);
  });

  it("finds entries intersecting a timeline range", () => {
    // A region touching the tail of u1 and the head of u2 matches both.
    expect(intersecting(base.transcript, 20_000, 30_000).map((item) => item.id)).toEqual([
      "u1",
      "u2",
    ]);
    // Boundaries are half-open, so an exact edge does not match the earlier entry.
    expect(intersecting(base.transcript, 24_000, 30_000).map((item) => item.id)).toEqual([
      "u2",
    ]);
  });

  it("reports the next entry in time order", () => {
    expect(neighbourAfter(base.transcript, "u1")?.id).toBe("u2");
    expect(neighbourAfter(base.transcript, "u3")).toBeNull();
  });
});

describe("add and delete", () => {
  it("adds a segment and keeps the list chronological", () => {
    const result = addSegment(base, 12_000, 18_000, "B", "interjection");
    expect(result.annotation.transcript.map((item) => item.id)).toEqual([
      "u1",
      result.id,
      "u2",
      "u3",
    ]);
    expect(result.annotation.transcript[1].text).toBe("interjection");
  });

  it("deletes a segment without reusing its id", () => {
    const next = deleteSegment(base, "u2");
    expect(next.transcript.map((item) => item.id)).toEqual(["u1", "u3"]);
    const added = addSegment(next, 24_000, 48_000, "A", "replacement");
    expect(added.id).not.toBe("u2");
  });

  it("leaves the original annotation untouched", () => {
    deleteSegment(base, "u2");
    expect(base.transcript).toHaveLength(3);
  });
});

describe("split", () => {
  it("splits into two adjacent ranges with new ids", () => {
    const result = splitSegment(base, "u1", 10_000, 5);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const [first, second] = result.ids;
    expect(first).not.toBe("u1");
    expect(second).not.toBe("u1");
    const parts = result.annotation.transcript.filter((item) =>
      result.ids.includes(item.id),
    );
    expect(parts[0].start_sample).toBe(0);
    expect(parts[0].end_sample).toBe(10_000);
    expect(parts[1].start_sample).toBe(10_000);
    expect(parts[1].end_sample).toBe(24_000);
    // Ranges stay adjacent and cover the original exactly.
    expect(parts[0].end_sample).toBe(parts[1].start_sample);
  });

  it("splits the text at the caret position", () => {
    const result = splitSegment(base, "u1", 10_000, 5);
    if (!result.ok) throw new Error("expected split");
    const parts = result.annotation.transcript.filter((item) =>
      result.ids.includes(item.id),
    );
    expect(parts[0].text).toBe("hello");
    expect(parts[1].text).toBe("there");
  });

  it("refuses a split point outside the segment", () => {
    expect(splitSegment(base, "u1", 24_000).ok).toBe(false);
    expect(splitSegment(base, "u1", 0).ok).toBe(false);
    const outside = splitSegment(base, "u1", 30_000);
    expect(outside.ok).toBe(false);
    if (!outside.ok) expect(outside.reason).toMatch(/inside/i);
  });

  it("changes nothing when the segment is missing", () => {
    const result = splitSegment(base, "nope", 10_000);
    expect(result.ok).toBe(false);
  });
});

describe("join", () => {
  it("joins same-speaker neighbours using union bounds and chronological text", () => {
    const result = joinSegments(base, "u1", "u2");
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const merged = result.annotation.transcript.find((item) => item.id === result.id)!;
    expect(merged.start_sample).toBe(0);
    expect(merged.end_sample).toBe(48_000);
    expect(merged.text).toBe("hello there how are you");
    expect(merged.speaker).toBe("A");
    expect(result.annotation.transcript).toHaveLength(2);
  });

  it("orders joined text chronologically regardless of argument order", () => {
    const result = joinSegments(base, "u2", "u1");
    if (!result.ok) throw new Error("expected join");
    const merged = result.annotation.transcript.find((item) => item.id === result.id)!;
    expect(merged.text).toBe("hello there how are you");
  });

  it("fails safely across speakers and changes nothing", () => {
    const result = joinSegments(base, "u2", "u3");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/same speaker/i);
    expect(base.transcript).toHaveLength(3);
  });

  it("merges flags from both halves", () => {
    const flagged = annotationWith([
      { ...base.transcript[0], quality_flags: ["repeated_ngram"] },
      { ...base.transcript[1], quality_flags: ["decode_disagreement"] },
    ]);
    const result = joinSegments(flagged, "u1", "u2");
    if (!result.ok) throw new Error("expected join");
    const merged = result.annotation.transcript.find((item) => item.id === result.id)!;
    expect(merged.quality_flags.sort()).toEqual([
      "decode_disagreement",
      "repeated_ngram",
    ]);
  });

  it("gives the joined segment a new id", () => {
    const result = joinSegments(base, "u1", "u2");
    if (!result.ok) throw new Error("expected join");
    expect(result.id).not.toBe("u1");
    expect(result.id).not.toBe("u2");
  });
});

describe("split and join round trip", () => {
  it("returns to the original bounds", () => {
    const split = splitSegment(base, "u1", 10_000, 5);
    if (!split.ok) throw new Error("expected split");
    const rejoined = joinSegments(split.annotation, split.ids[0], split.ids[1]);
    if (!rejoined.ok) throw new Error("expected join");
    const merged = rejoined.annotation.transcript.find((item) => item.id === rejoined.id)!;
    expect(merged.start_sample).toBe(0);
    expect(merged.end_sample).toBe(24_000);
    expect(merged.text).toBe("hello there");
  });
});

describe("flags and bounds", () => {
  it("toggles a flag on and off", () => {
    const on = toggleFlag(base, "u1", "overlapping_speech");
    expect(on.transcript[0].quality_flags).toEqual(["overlapping_speech"]);
    const off = toggleFlag(on, "u1", "overlapping_speech");
    expect(off.transcript[0].quality_flags).toEqual([]);
  });

  it("rejects invalid bounds", () => {
    expect(boundsError(0, 100, 1_000)).toBeNull();
    expect(boundsError(-1, 100, 1_000)).toMatch(/negative/i);
    expect(boundsError(100, 100, 1_000)).toMatch(/after start/i);
    expect(boundsError(0, 2_000, 1_000)).toMatch(/inside the source/i);
  });
});
