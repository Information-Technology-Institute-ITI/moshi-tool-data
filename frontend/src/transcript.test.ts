import { describe, expect, it } from "vitest";
import {
  addAllOverlapSegments,
  addOverlapSegments,
  addSegment,
  boundsError,
  chronological,
  deleteActivity,
  deleteSegment,
  intersecting,
  joinSegments,
  neighbourAfter,
  overlapsForAnnotation,
  segmentsForActivity,
  overlapsForSegment,
  resolveSplitSample,
  splitAllByTurns,
  splitSegment,
  splitSegmentByTurns,
  turnsForSegment,
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

  it("joins across speakers, keeping the earlier segment's speaker", () => {
    // Diarisation alternates speakers between neighbours, so refusing this
    // would leave Join with next permanently unavailable on real material.
    const result = joinSegments(base, "u2", "u3");
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const merged = result.annotation.transcript.find((item) => item.id === result.id)!;
    expect(merged.speaker).toBe("A");
    expect(merged.start_sample).toBe(24_000);
    expect(merged.end_sample).toBe(72_000);
    expect(merged.text).toBe("how are you quite well");
    expect(result.absorbedSpeaker).toBe("B");
  });

  it("names no absorbed speaker when both sides already matched", () => {
    const result = joinSegments(base, "u1", "u2");
    if (!result.ok) throw new Error("expected join");
    expect(result.absorbedSpeaker).toBeNull();
  });

  it("keeps the earlier speaker whichever order the arguments come in", () => {
    const result = joinSegments(base, "u3", "u2");
    if (!result.ok) throw new Error("expected join");
    const merged = result.annotation.transcript.find((item) => item.id === result.id)!;
    expect(merged.speaker).toBe("A");
    expect(merged.text).toBe("how are you quite well");
  });

  it("leaves the source annotation untouched", () => {
    joinSegments(base, "u2", "u3");
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

describe("split by speaker turns", () => {
  // The worked example: speaker A talks 1-2s and 2.5-5s on the timeline, but the
  // transcript holds one segment covering both rectangles.
  const S = 24_000;

  function laneSplit(text: string, words: { word: string; start: number; end: number }[]) {
    const annotation: Annotation = {
      ...annotationWith([utterance("u1", 1 * S, 5 * S, "A", text)]),
      activities: [
        { id: "act_1", speaker: "A", start_sample: 1 * S, end_sample: 2 * S, origin: "model" },
        { id: "act_2", speaker: "A", start_sample: 2.5 * S, end_sample: 5 * S, origin: "model" },
        { id: "act_3", speaker: "B", start_sample: 6 * S, end_sample: 7 * S, origin: "model" },
      ],
      aligned_words: words.map((word) => ({ ...word, speaker: "A" })),
    };
    return annotation;
  }

  const twoTurns = laneSplit("one two three", [
    { word: "one", start: 1.0, end: 1.8 },
    { word: "two", start: 2.6, end: 3.4 },
    { word: "three", start: 3.6, end: 4.9 },
  ]);

  it("lists only the rectangles on the segment's own speaker lane", () => {
    const turns = turnsForSegment(twoTurns.activities, twoTurns.transcript[0]);
    expect(turns).toEqual([
      { start_sample: 1 * S, end_sample: 2 * S },
      { start_sample: 2.5 * S, end_sample: 5 * S },
    ]);
  });

  it("keeps touching rectangles apart, because the timeline draws them apart", () => {
    const segment = utterance("u1", 0, 4 * S, "A", "");
    const turns = turnsForSegment(
      [
        { id: "a", speaker: "A", start_sample: 0, end_sample: 2 * S, origin: "model" },
        { id: "b", speaker: "A", start_sample: 2 * S, end_sample: 4 * S, origin: "model" },
      ],
      segment,
    );
    expect(turns).toHaveLength(2);
  });

  it("clips rectangles to the segment and merges overlapping ones", () => {
    const segment = utterance("u1", 1 * S, 5 * S, "A", "");
    const turns = turnsForSegment(
      [
        { id: "a", speaker: "A", start_sample: 0, end_sample: 2 * S, origin: "model" },
        { id: "b", speaker: "A", start_sample: 1.5 * S, end_sample: 3 * S, origin: "model" },
        { id: "c", speaker: "A", start_sample: 4 * S, end_sample: 9 * S, origin: "model" },
      ],
      segment,
    );
    // a and b touch, so they are one turn; c is clipped to the segment's end.
    expect(turns).toEqual([
      { start_sample: 1 * S, end_sample: 3 * S },
      { start_sample: 4 * S, end_sample: 5 * S },
    ]);
  });

  it("gives each rectangle its own segment with the words spoken in it", () => {
    const result = splitSegmentByTurns(twoTurns, "u1");
    expect(result.ok).toBe(true);
    if (!result.ok) return;

    const parts = result.annotation.transcript;
    expect(parts).toHaveLength(2);
    expect(parts[0].start_sample).toBe(1 * S);
    expect(parts[0].end_sample).toBe(2 * S);
    expect(parts[0].text).toBe("one");
    expect(parts[1].start_sample).toBe(2.5 * S);
    expect(parts[1].end_sample).toBe(5 * S);
    expect(parts[1].text).toBe("two three");
    expect(parts.every((part) => part.speaker === "A")).toBe(true);
  });

  it("divides across three rectangles as well as two", () => {
    const threeTurns: Annotation = {
      ...twoTurns,
      activities: [
        { id: "act_1", speaker: "A", start_sample: 1 * S, end_sample: 2 * S, origin: "model" },
        { id: "act_2", speaker: "A", start_sample: 2.5 * S, end_sample: 3.5 * S, origin: "model" },
        { id: "act_3", speaker: "A", start_sample: 3.5 * S, end_sample: 5 * S, origin: "model" },
      ],
    };
    const result = splitSegmentByTurns(threeTurns, "u1");
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.annotation.transcript.map((part) => part.text)).toEqual([
      "one",
      "two",
      "three",
    ]);
  });

  it("leaves the lanes untouched, because they are what it split against", () => {
    const result = splitSegmentByTurns(twoTurns, "u1");
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.annotation.activities).toEqual(twoTurns.activities);
  });

  it("gives a word in the silence between turns to the nearest one", () => {
    // "two" is spoken 2.35-2.45s, inside neither rectangle but closer to the
    // second, so it belongs with the words that follow it.
    const gapped = laneSplit("one two three", [
      { word: "one", start: 1.0, end: 1.8 },
      { word: "two", start: 2.35, end: 2.45 },
      { word: "three", start: 3.6, end: 4.9 },
    ]);
    const result = splitSegmentByTurns(gapped, "u1");
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.annotation.transcript.map((part) => part.text)).toEqual([
      "one",
      "two three",
    ]);
  });

  it("leaves a rectangle with no words spoken in it empty rather than dropping it", () => {
    const silent = laneSplit("one two", [
      { word: "one", start: 1.0, end: 1.4 },
      { word: "two", start: 1.5, end: 1.9 },
    ]);
    const result = splitSegmentByTurns(silent, "u1");
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.annotation.transcript.map((part) => part.text)).toEqual(["one two", ""]);
  });

  it("carries the flags onto every part and changes nothing else", () => {
    const flagged: Annotation = {
      ...twoTurns,
      transcript: [{ ...twoTurns.transcript[0], quality_flags: ["repeated_ngram"] }],
    };
    const result = splitSegmentByTurns(flagged, "u1");
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(
      result.annotation.transcript.every((part) =>
        part.quality_flags.includes("repeated_ngram"),
      ),
    ).toBe(true);
    // New ids, so undo history and revisions stay honest about what changed.
    expect(result.ids).toHaveLength(2);
    expect(result.ids).not.toContain("u1");
  });

  it("refuses when the segment sits on a single turn", () => {
    const single: Annotation = {
      ...twoTurns,
      activities: [
        { id: "act_1", speaker: "A", start_sample: 1 * S, end_sample: 5 * S, origin: "model" },
      ],
    };
    const result = splitSegmentByTurns(single, "u1");
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.reason).toMatch(/single speaker turn/);
  });

  it("refuses when the segment has no word timings", () => {
    const unaligned: Annotation = { ...twoTurns, aligned_words: [] };
    const result = splitSegmentByTurns(unaligned, "u1");
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.reason).toMatch(/no word timings/);
  });
});

describe("overlapping speech gets a segment for each speaker", () => {
  // The worked example: speaker A holds 10-20s, and the lanes show speaker B
  // talking over them from 16s to 18s.
  const S = 24_000;

  function overlapping(extra: Partial<Annotation> = {}): Annotation {
    return {
      ...annotationWith([utterance("u1", 10 * S, 20 * S, "A", "one two three four")]),
      activities: [
        { id: "act_a", speaker: "A", start_sample: 10 * S, end_sample: 20 * S, origin: "model" },
        { id: "act_b", speaker: "B", start_sample: 16 * S, end_sample: 18 * S, origin: "model" },
      ],
      aligned_words: [
        { word: "one", start: 10.5, end: 12.0, speaker: "A" },
        { word: "two", start: 16.2, end: 16.9, speaker: "A" },
        { word: "three", start: 17.1, end: 17.8, speaker: "A" },
        { word: "four", start: 18.5, end: 19.4, speaker: "A" },
      ],
      ...extra,
    };
  }

  it("reports the stretch where the other speaker is also talking", () => {
    const annotation = overlapping();
    expect(overlapsForSegment(annotation, annotation.transcript[0])).toEqual([
      { start_sample: 16 * S, end_sample: 18 * S, speaker: "B" },
    ]);
  });

  it("clips the overlap to the segment and merges touching regions", () => {
    const annotation = overlapping({
      activities: [
        { id: "a", speaker: "A", start_sample: 10 * S, end_sample: 20 * S, origin: "model" },
        { id: "b1", speaker: "B", start_sample: 5 * S, end_sample: 12 * S, origin: "model" },
        { id: "b2", speaker: "B", start_sample: 12 * S, end_sample: 14 * S, origin: "model" },
      ],
    });
    expect(overlapsForSegment(annotation, annotation.transcript[0])).toEqual([
      { start_sample: 10 * S, end_sample: 14 * S, speaker: "B" },
    ]);
  });

  it("adds a segment for the other speaker holding the words spoken there", () => {
    const annotation = overlapping();
    const result = addOverlapSegments(annotation, "u1");
    expect(result.ids).toHaveLength(1);

    const added = result.annotation.transcript.find((item) => item.id === result.ids[0])!;
    expect(added.speaker).toBe("B");
    expect(added.start_sample).toBe(16 * S);
    expect(added.end_sample).toBe(18 * S);
    expect(added.text).toBe("two three");
    expect(added.quality_flags).toContain("overlapping_speech");
    expect(added.alignment_status).toBe("aligned");
  });

  it("leaves the original segment and its words completely alone", () => {
    const annotation = overlapping();
    const result = addOverlapSegments(annotation, "u1");
    const original = result.annotation.transcript.find((item) => item.id === "u1")!;
    // Both people really were talking, so the words are copied, not moved.
    expect(original).toEqual(annotation.transcript[0]);
    expect(result.annotation.aligned_words).toEqual(annotation.aligned_words);
  });

  it("leaves the lanes untouched, because they are what it read", () => {
    const annotation = overlapping();
    const result = addOverlapSegments(annotation, "u1");
    expect(result.annotation.activities).toEqual(annotation.activities);
  });

  it("saves as both ranges: the full segment and the short overlap", () => {
    const annotation = overlapping();
    const result = addOverlapSegments(annotation, "u1");
    expect(
      result.annotation.transcript.map(
        (item) => `${item.speaker}:${item.start_sample}-${item.end_sample}`,
      ),
    ).toEqual([`A:${10 * S}-${20 * S}`, `B:${16 * S}-${18 * S}`]);
  });

  it("adds nothing the second time round", () => {
    const annotation = overlapping();
    const once = addOverlapSegments(annotation, "u1");
    const twice = addOverlapSegments(once.annotation, "u1");
    expect(twice.ids).toEqual([]);
    expect(twice.annotation.transcript).toHaveLength(2);
  });

  it("skips an overlap the other speaker already has a segment for", () => {
    const annotation = overlapping();
    const covered: Annotation = {
      ...annotation,
      transcript: [
        ...annotation.transcript,
        utterance("u2", 17 * S, 19 * S, "B", "already here"),
      ],
    };
    expect(overlapsForSegment(covered, covered.transcript[0])).toEqual([]);
  });

  it("adds an empty segment when no words were aligned inside the overlap", () => {
    const annotation = overlapping({ aligned_words: [] });
    const result = addOverlapSegments(annotation, "u1");
    const added = result.annotation.transcript.find((item) => item.id === result.ids[0])!;
    expect(added.text).toBe("");
    expect(added.alignment_status).toBe("not_run");
  });

  it("handles several overlapped stretches in one segment", () => {
    const annotation = overlapping({
      activities: [
        { id: "a", speaker: "A", start_sample: 10 * S, end_sample: 20 * S, origin: "model" },
        { id: "b1", speaker: "B", start_sample: 11 * S, end_sample: 12 * S, origin: "model" },
        { id: "b2", speaker: "B", start_sample: 16 * S, end_sample: 18 * S, origin: "model" },
      ],
    });
    const result = addOverlapSegments(annotation, "u1");
    expect(result.ids).toHaveLength(2);
    expect(
      result.annotation.transcript
        .filter((item) => item.speaker === "B")
        .map((item) => item.text),
    ).toEqual(["one", "two three"]);
  });

  it("counts every missing overlap across the transcript", () => {
    const annotation = overlapping();
    const listed = overlapsForAnnotation(annotation);
    expect(listed).toHaveLength(1);
    expect(listed[0].segment.id).toBe("u1");
    expect(listed[0].windows).toHaveLength(1);
    expect(overlapsForAnnotation(addOverlapSegments(annotation, "u1").annotation)).toEqual([]);
  });

  it("fills in every overlap in the transcript at once", () => {
    const annotation = overlapping({
      transcript: [
        utterance("u1", 10 * S, 20 * S, "A", "one two three four"),
        utterance("u2", 30 * S, 40 * S, "A", "later"),
      ],
      activities: [
        { id: "a1", speaker: "A", start_sample: 10 * S, end_sample: 20 * S, origin: "model" },
        { id: "b1", speaker: "B", start_sample: 16 * S, end_sample: 18 * S, origin: "model" },
        { id: "a2", speaker: "A", start_sample: 30 * S, end_sample: 40 * S, origin: "model" },
        { id: "b2", speaker: "B", start_sample: 33 * S, end_sample: 34 * S, origin: "model" },
      ],
    });
    const result = addAllOverlapSegments(annotation);
    expect(result.ids).toHaveLength(2);
    expect(result.annotation.transcript).toHaveLength(4);
    // Running it again is a no-op, so the button cannot pile up duplicates.
    expect(addAllOverlapSegments(result.annotation).ids).toEqual([]);
  });

  it("does not treat the same speaker's own neighbouring region as an overlap", () => {
    const annotation = overlapping({
      activities: [
        { id: "a1", speaker: "A", start_sample: 10 * S, end_sample: 15 * S, origin: "model" },
        { id: "a2", speaker: "A", start_sample: 15 * S, end_sample: 20 * S, origin: "model" },
      ],
    });
    expect(overlapsForSegment(annotation, annotation.transcript[0])).toEqual([]);
  });
});

describe("removing a speaker rectangle", () => {
  const S = 24_000;
  // Speaker A talks 0-4s across two rectangles, transcribed one segment each.
  // Speaker B talks 4-8s as one rectangle whose segment has not been split, so
  // a second B rectangle sits inside that same segment.
  const laned: Annotation = {
    ...annotationWith([
      utterance("a1", 0, 2 * S, "A", "first"),
      utterance("a2", 2 * S, 4 * S, "A", "second"),
      utterance("b1", 4 * S, 8 * S, "B", "one long turn"),
    ]),
    activities: [
      { id: "ra1", speaker: "A", start_sample: 0, end_sample: 2 * S, origin: "model" },
      { id: "ra2", speaker: "A", start_sample: 2 * S, end_sample: 4 * S, origin: "model" },
      { id: "rb1", speaker: "B", start_sample: 4 * S, end_sample: 6 * S, origin: "model" },
      { id: "rb2", speaker: "B", start_sample: 6 * S, end_sample: 8 * S, origin: "model" },
    ],
  };

  it("finds the segment a rectangle owns", () => {
    const region = laned.activities[0];
    expect(segmentsForActivity(laned.transcript, region).map((item) => item.id))
      .toEqual(["a1"]);
  });

  it("owns nothing when the segment spans other rectangles too", () => {
    // rb1 covers only half of b1, so b1 belongs to no single rectangle.
    const region = laned.activities[2];
    expect(segmentsForActivity(laned.transcript, region)).toEqual([]);
  });

  it("ignores segments on the other speaker's lane", () => {
    const region = { ...laned.activities[0], speaker: "B" as const };
    expect(segmentsForActivity(laned.transcript, region)).toEqual([]);
  });

  it("tolerates a rectangle resized a few samples off its segment", () => {
    const region = { ...laned.activities[0], start_sample: 60, end_sample: 2 * S - 60 };
    expect(segmentsForActivity(laned.transcript, region).map((item) => item.id))
      .toEqual(["a1"]);
  });

  it("removes the rectangle and the segment it owned", () => {
    const result = deleteActivity(laned, "ra1");
    expect(result.annotation.activities.map((item) => item.id))
      .toEqual(["ra2", "rb1", "rb2"]);
    expect(result.annotation.transcript.map((item) => item.id)).toEqual(["a2", "b1"]);
    expect(result.removedSegments.map((item) => item.id)).toEqual(["a1"]);
  });

  it("leaves an un-split spanning segment alone", () => {
    const result = deleteActivity(laned, "rb1");
    expect(result.annotation.activities.map((item) => item.id))
      .toEqual(["ra1", "ra2", "rb2"]);
    // The long B segment still covers rb2, so it survives.
    expect(result.annotation.transcript.map((item) => item.id))
      .toEqual(["a1", "a2", "b1"]);
    expect(result.removedSegments).toEqual([]);
  });

  it("removes an overlap segment with its own overlap rectangle", () => {
    const withOverlap: Annotation = {
      ...laned,
      transcript: [...laned.transcript, utterance("ov", 5 * S, 6 * S, "A", "cut in")],
      activities: [
        ...laned.activities,
        { id: "rov", speaker: "A", start_sample: 5 * S, end_sample: 6 * S, origin: "manual" },
      ],
    };
    const result = deleteActivity(withOverlap, "rov");
    expect(result.removedSegments.map((item) => item.id)).toEqual(["ov"]);
    expect(result.annotation.transcript.map((item) => item.id))
      .toEqual(["a1", "a2", "b1"]);
  });

  it("changes nothing when the rectangle is already gone", () => {
    const result = deleteActivity(laned, "missing");
    expect(result.annotation).toBe(laned);
    expect(result.removedSegments).toEqual([]);
  });

  it("leaves the source annotation untouched", () => {
    deleteActivity(laned, "ra1");
    expect(laned.activities).toHaveLength(4);
    expect(laned.transcript).toHaveLength(3);
  });
});

describe("dividing every segment by its speaker turns automatically", () => {
  const S = 24_000;

  /** One A segment drawn as three rectangles, and one B segment drawn as one. */
  function source(): Annotation {
    return {
      ...annotationWith([
        utterance("a", 0, 6 * S, "A", "one two three"),
        utterance("b", 6 * S, 8 * S, "B", "reply"),
      ]),
      activities: [
        { id: "r1", speaker: "A", start_sample: 0, end_sample: 2 * S, origin: "model" },
        { id: "r2", speaker: "A", start_sample: 2 * S, end_sample: 4 * S, origin: "model" },
        { id: "r3", speaker: "A", start_sample: 4 * S, end_sample: 6 * S, origin: "model" },
        { id: "r4", speaker: "B", start_sample: 6 * S, end_sample: 8 * S, origin: "model" },
      ],
      aligned_words: [
        { word: "one", start: 0.2, end: 1.5, speaker: "A" },
        { word: "two", start: 2.2, end: 3.5, speaker: "A" },
        { word: "three", start: 4.2, end: 5.5, speaker: "A" },
        { word: "reply", start: 6.2, end: 7.5, speaker: "B" },
      ],
    };
  }

  it("gives every rectangle its own transcription", () => {
    const result = splitAllByTurns(source());
    expect(result.dividedSegments).toBe(1);
    expect(result.addedSegments).toBe(2);
    expect(
      result.annotation.transcript.map(
        (item) => `${item.speaker}:${item.start_sample}-${item.end_sample}:${item.text}`,
      ),
    ).toEqual([
      `A:0-${2 * S}:one`,
      `A:${2 * S}-${4 * S}:two`,
      `A:${4 * S}-${6 * S}:three`,
      `B:${6 * S}-${8 * S}:reply`,
    ]);
  });

  it("converges, so opening the source again changes nothing", () => {
    const once = splitAllByTurns(source());
    const twice = splitAllByTurns(once.annotation);
    expect(twice.dividedSegments).toBe(0);
    expect(twice.annotation.transcript).toEqual(once.annotation.transcript);
  });

  it("leaves the lanes exactly as they were", () => {
    const before = source();
    const result = splitAllByTurns(before);
    expect(result.annotation.activities).toEqual(before.activities);
  });

  it("leaves a segment with no word timings alone", () => {
    const unaligned = { ...source(), aligned_words: [] };
    const result = splitAllByTurns(unaligned);
    expect(result.dividedSegments).toBe(0);
    expect(result.annotation.transcript).toHaveLength(2);
  });

  it("does not touch a segment that already matches one rectangle", () => {
    const result = splitAllByTurns(source());
    const reply = result.annotation.transcript.find((item) => item.speaker === "B")!;
    expect(reply.id).toBe("b");
  });

  it("leaves the source annotation untouched", () => {
    const before = source();
    splitAllByTurns(before);
    expect(before.transcript).toHaveLength(2);
  });
});

describe("bounds", () => {
  it("rejects invalid bounds", () => {
    expect(boundsError(0, 100, 1_000)).toBeNull();
    expect(boundsError(-1, 100, 1_000)).toMatch(/negative/i);
    expect(boundsError(100, 100, 1_000)).toMatch(/after start/i);
    expect(boundsError(0, 2_000, 1_000)).toMatch(/inside the source/i);
  });
});
