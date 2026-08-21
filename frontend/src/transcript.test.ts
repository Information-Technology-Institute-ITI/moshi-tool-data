import { describe, expect, it } from "vitest";
import {
  addSegment,
  boundsError,
  chronological,
  deleteSegment,
  intersecting,
  joinSegments,
  neighbourAfter,
  resolveSplitSample,
  splitSegment,
  toggleFlag,
  updateSegment,
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

describe("split follows word alignments", () => {
  // The worked example: one segment from 15.36s to 26.12s holding three words.
  const S = 24_000;
  const words = [
    { word: "word1", start: 15.36, end: 19, speaker: "A" },
    { word: "word2", start: 19, end: 24, speaker: "A" },
    { word: "word3", start: 24, end: 26.12, speaker: "A" },
  ];
  const withWords: Annotation = {
    ...annotationWith([
      utterance("u1", Math.round(15.36 * S), Math.round(26.12 * S), "A", "word1 word2 word3"),
    ]),
    aligned_words: words,
  };

  it("splits on a word boundary exactly where asked", () => {
    const result = splitSegment(withWords, "u1", Math.round(19 * S));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snappedFrom).toBeNull();
    expect(result.atSample).toBe(Math.round(19 * S));

    const parts = result.annotation.transcript;
    expect(parts[0].start_sample).toBe(Math.round(15.36 * S));
    expect(parts[0].end_sample).toBe(Math.round(19 * S));
    expect(parts[0].text).toBe("word1");
    expect(parts[1].start_sample).toBe(Math.round(19 * S));
    expect(parts[1].end_sample).toBe(Math.round(26.12 * S));
    expect(parts[1].text).toBe("word2 word3");
  });

  it("moves a mid-word split to the end of that word", () => {
    // 22s falls inside word2 (19-24), so the split moves to 24s and word2 stays
    // whole in the first half.
    const result = splitSegment(withWords, "u1", Math.round(22 * S));
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.snappedFrom).toBe(Math.round(22 * S));
    expect(result.atSample).toBe(Math.round(24 * S));

    const parts = result.annotation.transcript;
    expect(parts[0].end_sample).toBe(Math.round(24 * S));
    expect(parts[0].text).toBe("word1 word2");
    expect(parts[1].start_sample).toBe(Math.round(24 * S));
    expect(parts[1].end_sample).toBe(Math.round(26.12 * S));
    expect(parts[1].text).toBe("word3");
  });

  it("never leaves a word straddling the boundary", () => {
    for (const at of [16, 17, 18, 20, 22, 23, 24.5, 25]) {
      const result = splitSegment(withWords, "u1", Math.round(at * S));
      if (!result.ok) continue;
      const boundary = result.atSample / S;
      for (const word of words) {
        expect(word.start < boundary && boundary < word.end).toBe(false);
      }
    }
  });

  it("refuses when the covering word runs to the end of the segment", () => {
    // 25s is inside word3, which ends with the segment, so nothing would follow.
    const result = splitSegment(withWords, "u1", Math.round(25 * S));
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/nothing after it/i);
  });

  it("keeps every word across a split", () => {
    const result = splitSegment(withWords, "u1", Math.round(22 * S));
    if (!result.ok) throw new Error("expected split");
    const combined = result.annotation.transcript.map((item) => item.text).join(" ");
    expect(combined).toBe("word1 word2 word3");
  });

  it("only considers words inside the segment being split", () => {
    const neighbours = {
      ...withWords,
      aligned_words: [
        { word: "before", start: 10, end: 15, speaker: "A" },
        ...words,
        { word: "after", start: 27, end: 30, speaker: "A" },
      ],
    };
    const result = splitSegment(neighbours, "u1", Math.round(19 * S));
    if (!result.ok) throw new Error("expected split");
    const combined = result.annotation.transcript.map((item) => item.text).join(" ");
    expect(combined).not.toContain("before");
    expect(combined).not.toContain("after");
  });

  it("falls back to the text cursor when the segment has no word timings", () => {
    const result = splitSegment(base, "u1", 10_000, 5);
    if (!result.ok) throw new Error("expected split");
    const parts = result.annotation.transcript.filter((item) =>
      result.ids.includes(item.id),
    );
    expect(parts[0].text).toBe("hello");
    expect(parts[1].text).toBe("there");
    expect(result.snappedFrom).toBeNull();
  });

  it("resolves the split point without mutating anything", () => {
    const resolved = resolveSplitSample(
      words,
      Math.round(15.36 * S),
      Math.round(26.12 * S),
      Math.round(22 * S),
    );
    expect(resolved.sample).toBe(Math.round(24 * S));
    expect(resolved.snappedFrom).toBe(Math.round(22 * S));
    expect(withWords.transcript).toHaveLength(1);
  });

  it("places words with missing timings alongside their neighbours", () => {
    const gappy = {
      ...withWords,
      aligned_words: [
        { word: "word1", start: 15.36, end: 19 },
        { word: "unaligned", start: null, end: null },
        { word: "word3", start: 24, end: 26.12 },
      ],
    };
    const result = splitSegment(gappy, "u1", Math.round(19 * S));
    if (!result.ok) throw new Error("expected split");
    // The unaligned word is not dropped; it stays in the text somewhere.
    const combined = result.annotation.transcript.map((item) => item.text).join(" ");
    expect(combined).toContain("word1");
    expect(combined).toContain("word3");
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

describe("speaker lanes follow the transcript", () => {
  // The timeline lanes render annotation.activities, so every structural
  // transcript edit has to move them or the timeline goes stale.
  const laned: Annotation = {
    ...base,
    activities: [
      { id: "act_1", speaker: "A", start_sample: 0, end_sample: 24_000, origin: "model" },
      { id: "act_2", speaker: "A", start_sample: 24_000, end_sample: 48_000, origin: "model" },
      { id: "act_3", speaker: "B", start_sample: 48_000, end_sample: 72_000, origin: "model" },
    ],
  };

  function ranges(annotation: Annotation) {
    return annotation.activities.map(
      (item) => `${item.speaker}:${item.start_sample}-${item.end_sample}`,
    );
  }

  it("adds a lane region when a segment is added", () => {
    const result = addSegment(laned, 72_000, 80_000, "B", "new");
    expect(result.annotation.activities).toHaveLength(4);
    expect(ranges(result.annotation)).toContain("B:72000-80000");
  });

  it("removes the lane region when a segment is deleted", () => {
    const next = deleteSegment(laned, "u2");
    expect(next.activities).toHaveLength(2);
    expect(ranges(next)).not.toContain("A:24000-48000");
    // Unrelated lanes survive.
    expect(ranges(next)).toContain("A:0-24000");
    expect(ranges(next)).toContain("B:48000-72000");
  });

  it("splits the lane region into two when a segment is split", () => {
    const result = splitSegment(laned, "u1", 10_000, 5);
    if (!result.ok) throw new Error("expected split");
    expect(result.annotation.activities).toHaveLength(4);
    expect(ranges(result.annotation)).toContain("A:0-10000");
    expect(ranges(result.annotation)).toContain("A:10000-24000");
    expect(ranges(result.annotation)).not.toContain("A:0-24000");
  });

  it("merges the lane regions when segments are joined", () => {
    const result = joinSegments(laned, "u1", "u2");
    if (!result.ok) throw new Error("expected join");
    expect(result.annotation.activities).toHaveLength(2);
    expect(ranges(result.annotation)).toContain("A:0-48000");
    expect(ranges(result.annotation)).not.toContain("A:0-24000");
    expect(ranges(result.annotation)).not.toContain("A:24000-48000");
  });

  it("moves the lane region when a segment is retimed", () => {
    const next = updateSegment(laned, "u1", { start_sample: 2_000, end_sample: 20_000 });
    expect(ranges(next)).toContain("A:2000-20000");
    expect(ranges(next)).not.toContain("A:0-24000");
  });

  it("moves the region to the other lane when the speaker changes", () => {
    const next = updateSegment(laned, "u1", { speaker: "B" });
    expect(ranges(next)).toContain("B:0-24000");
    expect(ranges(next)).not.toContain("A:0-24000");
  });

  it("leaves the lanes alone when only text changes", () => {
    const next = updateSegment(laned, "u1", { text: "edited words" });
    expect(next.activities).toEqual(laned.activities);
  });

  it("keeps lane regions sorted by time", () => {
    const result = addSegment(laned, 12_000, 16_000, "B", "interjection");
    const starts = result.annotation.activities.map((item) => item.start_sample);
    expect(starts).toEqual([...starts].sort((left, right) => left - right));
  });

  it("keeps the lanes and the transcript the same length through edits", () => {
    let current = laned;
    const split = splitSegment(current, "u1", 10_000, 5);
    if (!split.ok) throw new Error("expected split");
    current = split.annotation;
    expect(current.activities).toHaveLength(current.transcript.length);

    current = deleteSegment(current, split.ids[0]);
    expect(current.activities).toHaveLength(current.transcript.length);

    const added = addSegment(current, 80_000, 90_000, "A", "tail");
    expect(added.annotation.activities).toHaveLength(added.annotation.transcript.length);
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
